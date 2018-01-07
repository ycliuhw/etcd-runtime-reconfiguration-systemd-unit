#!/usr/bin/env python

import os
import logging

import boto3
from requests import get, post, delete
# import envoy

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

VAR_PREFIX = 'ETCD_RECONFIG_'
ETCDCTL_PATH = '/etcdctl'
META_DATA_FILE_NAME = '/run/metadata/etcd'

asg_client = boto3.client('autoscaling')
ec2_client = boto3.client('ec2')
elb_client = boto3.client('elb')


class ClusterCrashError(Exception):
    """cluster is totally broken and quorum cannot be restored, it needs to rebuild and retore data manually
    """
    pass


class EtcdCluster(object):

    data = None
    etcdctl_cmd = None
    etcd_api_uri = None
    cached_props = None

    def __init__(self, asg_name):
        self.cached_props = self.cached_props or {}

        asg_meta = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])['AutoScalingGroups'][0]
        # ec2_meta = ec2_client.describe_instances(
        #     Filters=[{'Name': 'instance-id', 'Values': [i['InstanceId'] for i in asg_meta['Instances']]}]
        # )['Reservations']
        elb_meta = elb_client.describe_load_balancers(
            LoadBalancerNames=asg_meta['LoadBalancerNames']
        )['LoadBalancerDescriptions'][0]

        # instances_ips = [
        #     {
        #         k: v
        #         for k, v in ec2['Instances'][0].items()
        #         if k in ('PublicIpAddress', 'PrivateIpAddress')
        #     }
        #     for ec2 in ec2_meta
        # ]
        elb_dns = self._dns2endpoint(elb_meta['DNSName'])

        cluster_endpoint = ','.join([elb_dns + ':%s' % port for port in (2379, 4001)])
        self.etcdctl_cmd = ETCDCTL_PATH + ' --endpoints=%s' % cluster_endpoint
        self.etcd_api_uri = elb_dns + ':2379/v2/members'
        self.data = {
            VAR_PREFIX + 'ENDPOINTS': cluster_endpoint,
            VAR_PREFIX + 'NAME': self.local_ipv4
        }
        logger.info('data -> \n%s', self.data)
        self()

    def _prepare_metadata(self):
        members = self.list_member()
        self.data[VAR_PREFIX + 'INITIAL_CLUSTER'] = ','.join(
            [
                '{id}={dns}'.format(id=member['id'], dns=member['peerURLs'][0])
                for member in members
            ]
        )
        self.data[VAR_PREFIX + 'INITIAL_CLUSTER_STATE'] = 'existing'

    def ensure_metadata(self):
        self._prepare_metadata()
        metadata = '\n'.join(['{k}={v}'.format(k=k, v=v) for k, v in self.data.items()])
        logger.info('writing metadata to `%s` -> \n%s', META_DATA_FILE_NAME, metadata)
        with open(META_DATA_FILE_NAME, 'w') as f:
            f.write(metadata)

    def _dns2endpoint(self, dns):
        # we do not enable ssl for now
        if not dns.startswith('http://'):
            dns = 'http://' + dns
        return dns.rstrip('/')

    def add_member(self, ip=None):
        ip = ip or self.local_ipv4
        # cmd = self.etcdctl_cmd + ' member add {name} http://{ip}:2380'.format(name=ip, ip=ip)
        # logger.info('add_member cmd -> %s', cmd)
        # r = envoy.run(cmd)
        # if r.status_code != 0:
        #     raise Exception(r.std_err)
        # logger.info('add_member %s -> \n%s', ip, r.std_out)
        payload = dict(peerURLs=["http://%s:2380" % ip], name=ip)
        logger.info('add_member payload -> %s', payload)
        r = post(self.etcd_api_uri, json=payload)
        logger.info('add_member %s -> \n%s', ip, r.json())

    def _cleanup_bad_member(self):
        healthy_members = []
        unhealthy_members = []
        for member in self.list_member():
            if self.is_member_healthy(member):
                healthy_members.append(member)
            else:
                unhealthy_members.append(member)

        if len(healthy_members) < 2:
            raise ClusterCrashError('healthy member is less than 2!!!')

        for member in unhealthy_members:
            logger.warn('Unhealthy member found ->%s, removing it now!', member)
            self.remove_member(id=member['id'])

    def is_member_healthy(member):
        try:
            client_url = member['clientURLs'][0]
        except KeyError:
            logger.error('No `clientURLs` for member -> %s', member)
            return False

        try:
            return get(client_url + '/health').json()['health'] == 'true'
        except Exception as e:
            logger.error('`is_member_healthy` check failed, error -> %s, member -> %s', e, member)
            return False

    def list_member(self):
        return get(self.etcd_api_uri).json()['members']

    def remove_member(self, id):
        r = delete(self.etcd_api_uri + '/%s' % id)
        logger.info('remove_member %s -> \n%s', id, r.json())

    @property
    def local_ipv4(self):
        if not self.cached_props.get('local_ipv4'):
            self.cached_props['local_ipv4'] = get('http://169.254.169.254/2016-09-02/meta-data/local-ipv4').text
        return self.cached_props['local_ipv4']

    def __call__(self):
        self.add_member()
        self.ensure_metadata()


if __name__ == '__main__':

    asg_name = os.environ.get('ASG_NAME', None)

    # ensure `ASG_NAME` specified
    if asg_name is None:
        raise Exception('`ASG_NAME` is required in env')
    # ensure `etcdctl` is accessible
    if not os.path.isfile(ETCDCTL_PATH) or not os.access(ETCDCTL_PATH, os.X_OK):
        raise Exception('%s is not executable!!!' % ETCDCTL_PATH)
    EtcdCluster(asg_name)
