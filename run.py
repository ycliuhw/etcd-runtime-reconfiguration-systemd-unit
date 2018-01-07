#!/usr/bin/env python

import os
import logging

import boto3
from requests import get
import envoy

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

VAR_PREFIX = 'ETCD_RECONFIG'
ETCDCTL_PATH = '/etcdctl'

asg_client = boto3.client('autoscaling')
ec2_client = boto3.client('ec2')
elb_client = boto3.client('elb')


class EtcdClusterMeta(object):

    data = None
    etcdctl_cmd = None
    cached_props = None

    def __init__(self, asg_name):
        self.cached_props = self.cached_props or {}

        asg_meta = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])['AutoScalingGroups'][0]
        ec2_meta = ec2_client.describe_instances(
            Filters=[{'Name': 'instance-id', 'Values': [i['InstanceId'] for i in asg_meta['Instances']]}]
        )['Reservations']
        elb_meta = elb_client.describe_load_balancers(
            LoadBalancerNames=asg_meta['LoadBalancerNames']
        )['LoadBalancerDescriptions'][0]

        instances_ips = [
            {
                k: v
                for k, v in ec2['Instances'][0].items()
                if k in ('PublicIpAddress', 'PrivateIpAddress')
            }
            for ec2 in ec2_meta
        ]
        elb_dns = elb_meta['DNSName']

        cluster_endpoint = ','.join([self._dns2endpoint(elb_dns) + ':%s' % port for port in (2379, 4001)])
        self.etcdctl_cmd = ETCDCTL_PATH + ' --endpoints=%s' % cluster_endpoint
        self.data = {
            VAR_PREFIX + 'ENDPOINTS': cluster_endpoint
        }
        logger.info('data -> \n%s', self.data)

    def _dns2endpoint(self, dns):
        # we do not enable ssl for now
        if not dns.startswith('http://'):
            dns = 'http://' + dns
        return dns.rstrip('/')

    def add_member(self, ip=None):
        ip = ip or self.local_ipv4
        cmd = self.etcdctl_cmd + ' member add {name} http://{ip}:2380'.format(name=ip, ip=ip)
        r = envoy.run(cmd)
        if r.status_code != 0:
            raise Exception(r.std_err)
        logger.info('add_member %s -> \n%s', ip, r.std_out)

    @property
    def local_ipv4(self):
        if not self.cached_props.get('local_ipv4'):
            self.cached_props['local_ipv4'] = get('http://169.254.169.254/2016-09-02/meta-data/local-ipv4').text
        return self.cached_props['local_ipv4']


if __name__ == '__main__':

    asg_name = os.environ.get('ASG_NAME', None)

    # ensure `ASG_NAME` specified
    if asg_name is None:
        raise Exception('`ASG_NAME` is required in env')
    # ensure `etcdctl` is accessible
    if not os.path.isfile(ETCDCTL_PATH) or not os.access(ETCDCTL_PATH, os.X_OK):
        raise Exception('%s is not executable!!!' % ETCDCTL_PATH)
    etcd_cluster_meta = EtcdClusterMeta(asg_name)
