#!/usr/bin/env python

import os
import logging
from enum import Enum
from uuid import uuid4

import boto3
from requests import get, post, delete
# import envoy

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


class ClusterState(Enum):
    NEW = 'new'
    EXISTING = 'existing'


VAR_PREFIX = 'K_ETCD_'
ETCDCTL_PATH = '/usr/bin/etcdctl'
META_DATA_FILE_NAME = '/run/metadata/etcd'

asg_client = boto3.client('autoscaling')
# ec2_client = boto3.client('ec2')
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

    def _prepare_metadata(self, state):
        if state == ClusterState.NEW:
            ENV_CLUSTER = {
                'ETCD_ADVERTISE_CLIENT_URLS': 'http://{PRIVATE_IPV4}:2379',
                'ETCD_INITIAL_ADVERTISE_PEER_URLS': 'http://{PRIVATE_IPV4}:2380',
                'ETCD_LISTEN_CLIENT_URLS': 'http://0.0.0.0:2379,http://0.0.0.0:4001',
                'ETCD_LISTEN_PEER_URLS': 'http://{PRIVATE_IPV4}:2380',
                'ETCD_DISCOVERY': None,
            }

            ENV_CLUSTER = {
                os.environ.get(k, None) if v is None else v
                for k, v in ENV_CLUSTER.items()
            }
        else:
            members = self.list_member()
            ENV_CLUSTER = {
                'ETCD_NAME': uuid4(),
                'ETCD_INITIAL_CLUSTER': ','.join(
                    [
                        '{id}={dns}'.format(id=member['id'], dns=member['peerURLs'][0])
                        for member in members
                    ]
                ),
                'ETCD_INITIAL_CLUSTER_STATE': ClusterState.EXISTING,
            }
        self.data.update(ENV_CLUSTER)

    def validate_metadata(self, data):
        for k, v in data.items():
            if not v:
                raise ValueError('validate_metadata, `%s=%s` was missing!', k, v)
        return data

    def ensure_metadata(self, state):
        self._prepare_metadata(state)
        metadata = '\n'.join(['{k}={v}'.format(k=k, v=v) for k, v in self.validate_metadata(self.data).items()])
        logger.info('writing metadata (state -> `%s`) to `%s` -> \n%s', state, META_DATA_FILE_NAME, metadata)
        with open(META_DATA_FILE_NAME, 'w') as f:
            f.write(metadata)

    def _dns2endpoint(self, dns):
        # we do not enable ssl for now
        if not dns.startswith('http://'):
            dns = 'http://' + dns
        return dns.rstrip('/')

    def add_member(self, ip=None):
        # do cleanup before add member
        self._cleanup_bad_member()

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
        logger.info('add_member %s -> \n%s, %s', ip, r.status_code, r.json())

    def _cleanup_bad_member(self):
        unhealthy_members = [m for m in self.list_member() if m['is_healthy'] is False]
        for member in unhealthy_members:
            logger.warn('Unhealthy member found ->%s, removing it now!', member)
            self.remove_member(id=member['id'])

    def is_member_healthy(self, member):
        try:
            client_url = member['clientURLs'][0]
        except (KeyError, IndexError):
            # new member needs to be ignored (neither healthy nor unhealthy)
            logger.warn('No `clientURLs` for member(it is probably a newly added member) -> %s', member)
            return None

        try:
            return get(client_url + '/health').json()['health'] == 'true'
        except Exception as e:
            logger.error('`is_member_healthy` check failed, error -> %s, member -> %s', e, member)
            return False

    def get_cluster_state(self):

        try:
            current_members = self.list_member()
            state = ClusterState.EXISTING
            logger.info('get_cluster_state: cluster state -> %s, current_members -> %s', state, current_members)
        except IOError as e:
            state = ClusterState.NEW
            logger.warn('get_cluster_state: error -> %s, so state -> %s', e, state)
        return state

    def list_member(self):
        members = get(self.etcd_api_uri).json()['members']
        for member in members:
            member['is_healthy'] = self.is_member_healthy(member)

        healthy_members = [m for m in members if m['is_healthy'] is True]
        if len(members) >= 2 and len(healthy_members) < 2:
            raise ClusterCrashError('Quorum lost: healthy members are less than 2!!!')
        return members

    def remove_member(self, id):
        r = delete(self.etcd_api_uri + '/%s' % id)
        logger.info('remove_member %s -> \n%s', id, r.json())

    @property
    def local_ipv4(self):
        if not self.cached_props.get('local_ipv4'):
            self.cached_props['local_ipv4'] = get('http://169.254.169.254/2016-09-02/meta-data/local-ipv4').text
        return self.cached_props['local_ipv4']

    def __call__(self):
        state = self.get_cluster_state()
        if state == ClusterState.EXISTING:
            # this is ONLY for existing cluster - `runtime reconfiguration`
            logger.info(
                '%s cluster, so doing 1. `add_member`, 2. `ensure_metadata` for reconfiguration later...', state
            )
            self.add_member()
        else:
            logger.info('%s cluster, nothing to do with it, ignoring...', state)

        self.ensure_metadata(state=state)


if __name__ == '__main__':

    asg_name = os.environ.get('ASG_NAME', None)
    discovery_url = os.environ.get('ETCD_DISCOVERY', None)

    # ensure `ASG_NAME` specified
    if asg_name is None or discovery_url is None:
        raise Exception('`ASG_NAME` `ETCD_DISCOVERY` are required in env')
    # ensure `etcdctl` is accessible
    if not os.path.isfile(ETCDCTL_PATH) or not os.access(ETCDCTL_PATH, os.X_OK):
        raise Exception('%s is not executable!!!' % ETCDCTL_PATH)
    EtcdCluster(asg_name=asg_name)
