# -*- coding: utf-8 -*-
"""
Celery ASG

Usage:
  celery-asg --asg-name <auto-scaling-group-name> --broker <celery-broker-url>

Options:
  -h --help      Show this screen.
  --version      Show version.
"""

from docopt import docopt
from celeryasg import __version__, run


def run(asg_name, broker):
    celery = CeleryASG(asg_name=asg_name, broker=broker)
    inactive_instances = celery.find_inactive_instances()
    for instance in inactive_instances:
        print('Shuting down: {}'.format(instance['InstanceId']))
        celery.shutdown_instance(instance)

    print('Auto balancing ASG...')
    n = celery.auto_balance()
    if n:
        print('Desired changed to {}'.format(n))


def entrypoint():
    args = docopt(__doc__, version=__version__)
    run(args['<auto-scaling-group-name>'],
        args['<celery-broker-url>'])


if __name__ == '__main__':
    entrypoint()
