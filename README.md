# etcd-runtime-reconfiguration-systemd-unit
service for runtime reconfiguration for existing etcd cluster

Prepare metadata as below for new member to boot up then join cluster

```
/ # cat /run/metadata/etcd
ETCD_RECONFIG_ENDPOINTS=http://internal-p-etcd-EtcdElb-VK9ALY39P25I-1745127484.ap-southeast-2.elb.amazonaws.com:2379,http://internal-p-etcd-EtcdElb-VK9ALY39P25I-1745127484.ap-southeast-2.elb.amazonaws.com:4001
ETCD_RECONFIG_NAME=10.128.4.132
ETCD_RECONFIG_INITIAL_CLUSTER=2bd7035aa791e873=http://10.128.1.214:2380,7dc0b97b2507b31c=http://10.128.4.132:2380,a824b620ece3ec0d=http://10.128.2.161:2380
ETCD_RECONFIG_INITIAL_CLUSTER_STATE=existing/
```

```
[Unit]
Description=ETCD metdata agent
Requires=metadata.service
Requires=docker.service
After=metadata.service
After=docker.service

[Service]
Type=oneshot
EnvironmentFile=/run/metadata/ec2
ExecStartPre=-/usr/bin/docker stop etcd-runtime-reconfiguration-systemd-unit
ExecStartPre=-/usr/bin/docker rm -f etcd-runtime-reconfiguration-systemd-unit
ExecStartPre=/usr/bin/docker pull ycliuhw/etcd-runtime-reconfiguration-systemd-unit
ExecStart=/usr/bin/docker run --name=etcd-runtime-reconfiguration-systemd-unit -v /run/metadata:/run/metadata -e ASG_NAME=${AWS_TAG_ASG_GROUPNAME} -e AWS_DEFAULT_REGION=${AWS_REGION} -v $(which etcdctl):/etcdctl ycliuhw/etcd-runtime-reconfiguration-systemd-unit:latest

[Install]
WantedBy=multi-user.target
```
