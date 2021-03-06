# -*- coding: utf-8 -*-

import os
import math
import pytz
import boto3
from datetime import datetime, timedelta
from celery import Celery


class CeleryASG(Celery):

    def __init__(self, asg_name, aws_region=None, queue_name=None, *args, **kwargs):
        super(CeleryASG, self).__init__(*args, **kwargs)
        self.asg_name = asg_name
        self.aws_region = aws_region
        self.queue_name = queue_name if queue_name else os.getenv('CELERY_DEFAULT_QUEUE', 'celery')
        self._inspector = None

    @property
    def inspector(self):
        if self._inspector is None:
            self._inspector = self.control.inspect()
        return self._inspector

    def get_active_workers(self):
        return self.inspector.active()

    def get_pending_count(self):
        with self.inspector.app.connection_or_acquire() as conn:
            queue = conn.default_channel.queue_declare(queue=self.queue_name, passive=True)
            return queue.message_count

    def find_inactive_instances(self, cooldown_period=300):
        if self.get_pending_count() > 0:
            return []

        ec2_instances = self.list_running_ec2_instances()
        active_workers = self.get_active_workers() or {}

        for ec2_instance in ec2_instances:
            ec2_instance['workers'] = []
            for worker_name, tasks in active_workers.items():
                _, ip = worker_name.split('@', 1)
                if ec2_instance['PublicIp'] == ip:
                    for task in tasks:
                        ec2_instance['workers'].append(task)

        inactive_instances = [i for i in ec2_instances if not i['workers']]

        if cooldown_period is not None:
            t0 = (datetime.utcnow() - timedelta(seconds=cooldown_period)).replace(tzinfo=pytz.utc)
            inactive_instances = [i for i in inactive_instances if i['LaunchTime'] < t0]

        return inactive_instances

    def list_running_ec2_instances(self):
        asg_client = boto3.client('autoscaling', region_name=self.aws_region)

        asg_instances = self._asg_instances()
        if not asg_instances:
            return []

        ec2_client = boto3.client('ec2', region_name=self.aws_region)
        ec2_instances = ec2_client.describe_instances(InstanceIds=[i['InstanceId'] for i in asg_instances])

        def _get_public_ip(instance):
            for iface in instance.get('NetworkInterfaces', []):
                if iface['Association']:
                    return iface['Association']['PublicIp']

        running_instances = []
        for reservation in ec2_instances['Reservations']:
            for instance in reservation['Instances']:
                if instance['State']['Name'] != 'running':
                    continue

                public_ip = _get_public_ip(instance)
                running_instances.append({
                    'PublicIp': public_ip,
                    'PublicDns': instance['PublicDnsName'],
                    'InstanceId': instance['InstanceId'],
                    'LaunchTime': instance['LaunchTime'],
                    'AutoScalingGroupName': self.asg_name
                })
        return running_instances

    def shutdown_instance(self, instance, dryrun=False):
        assert instance is not None and instance != []

        if dryrun:
            print('Shuting down instance: {}'.format(repr(instance)))
            return

        asg_client = boto3.client('autoscaling', region_name=self.aws_region)
        return asg_client.terminate_instance_in_auto_scaling_group(
                 InstanceId=instance['InstanceId'],
                 ShouldDecrementDesiredCapacity=True)

    def auto_balance(self, factor=0.5, dryrun=False):
        asg_client = boto3.client('autoscaling', region_name=self.aws_region)
        asg_instances = self._asg_instances()

        instances_count = len(asg_instances)
        messages_count = self.get_pending_count()

        if messages_count * factor > instances_count:
            new_desired = math.ceil(messages_count * factor)
            if dryrun:
                print('Desired: {}'.format(new_desired))
            else:
                self.set_asg_desired(new_desired)
            return new_desired

    def set_asg_desired(self, n):
        asg_client = boto3.client('autoscaling', region_name=self.aws_region)
        info = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[self.asg_name])
        if not info['AutoScalingGroups']:
            raise RuntimeError('Auto Scaling Group "{}" not found'.format(self.asg_name))

        n = min(n, info['AutoScalingGroups'][0]['MaxSize'])
        asg_client.set_desired_capacity(DesiredCapacity=n, AutoScalingGroupName=self.asg_name)

        return n

    def _asg_instances(self):
        asg_client = boto3.client('autoscaling', region_name=self.aws_region)

        asg_instances = []
        asg_instances_paginator = asg_client.get_paginator('describe_auto_scaling_instances')
        for response in asg_instances_paginator.paginate():
            for instance in response['AutoScalingInstances']:
                if instance['AutoScalingGroupName'] == self.asg_name:
                    asg_instances.append(instance)
        return asg_instances
