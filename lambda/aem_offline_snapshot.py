# -*- coding: utf8 -*-

"""
Lambda function to manage AEM stack offline backups. It uses a SNS topic to help
orchestrate sequence of steps
"""

import os
import boto3
import logging
import json
import datetime


__author__ = 'Andy Wang (andy.wang@shinesolutions.com)'
__copyright__ = 'Shine Solutions'
__license__ = 'Apache License, Version 2.0'


# setting up logger
logger = logging.getLogger(__name__)
logger.setLevel(int(os.getenv('LOG_LEVEL', logging.INFO)))


# AWS resources
ssm = boto3.client('ssm')
ec2 = boto3.client('ec2')
s3 = boto3.client('s3')
dynamodb = boto3.client('dynamodb')
sns = boto3.client('sns')


class MyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        return json.JSONEncoder.default(self, obj)


def instance_ids_by_tags(filters):
    response = ec2.describe_instances(
        Filters=filters
    )
    response2 = json.loads(json.dumps(response, cls=MyEncoder))

    instance_ids = []
    for reservation in response2['Reservations']:
        instance_ids += [instance['InstanceId'] for instance in reservation['Instances']]
    return instance_ids


def send_ssm_cmd(cmd_details):
    print('calling ssm commands')
    return json.loads(json.dumps(ssm.send_command(**cmd_details),
                      cls=MyEncoder))


def get_author_primary_ids(stack_prefix):
    filters = [
        {
            'Name': 'tag:StackPrefix',
            'Values': [stack_prefix]
        }, {
            'Name': 'instance-state-name',
            'Values': ['running']
        }, {
            'Name': 'tag:Component',
            'Values': ['author-primary']
        }
    ]

    return instance_ids_by_tags(filters)


def get_author_standby_ids(stack_prefix):
    filters = [
        {
            'Name': 'tag:StackPrefix',
            'Values': [stack_prefix]
        }, {
            'Name': 'instance-state-name',
            'Values': ['running']
        }, {
            'Name': 'tag:Component',
            'Values': ['author-standby']
        }
    ]

    return instance_ids_by_tags(filters)


def get_publish_ids(stack_prefix):
    filters = [
        {
            'Name': 'tag:StackPrefix',
            'Values': [stack_prefix]
        }, {
            'Name': 'instance-state-name',
            'Values': ['running']
        }, {
            'Name': 'tag:Component',
            'Values': ['publish']
        }
    ]

    return instance_ids_by_tags(filters)


def stack_health_check(stack_prefix, min_publish_instances):
    """
    Simple AEM stack health check based on the number of author-primary,
    author-standby and publisher instances.
    """

    author_primary_instances = get_author_primary_ids(stack_prefix)

    author_standby_instances = get_author_standby_ids(stack_prefix)

    publish_instances = get_publish_ids(stack_prefix)

    if (len(author_primary_instances) == 1 and
       len(author_standby_instances) == 1 and
       len(publish_instances) >= min_publish_instances):
        return {
          'author-primary': author_primary_instances[0],
          'author-standby': author_standby_instances[0],
          'publish': publish_instances[0]
        }

    if len(author_primary_instances) != 1:
        logger.error('Found {} author-primary instances. Unhealthy stack.'.format(len(author_primary_instances)))

    if len(author_standby_instances) != 1:
        logger.error('Found {} author-standby instances. Unhealthy stack.'.format(len(author_standby_instances)))

    if len(publish_instances) < min_publish_instances:
        logger.error('Found {} publish instances. Unhealthy stack.'.format(len(publish_instances)))

    return None


def put_state_in_dynamodb(table_name, command_id, environment, state, timestamp, **kwargs):

    """
    schema:
    key: command_id, ec2 run command id, or externalId if provided and no cmd
         has ran yet
    attr:
      environment: S usually stack_prefix
      state: S STOP_AUTHOR_STANDBY, STOP_AUTHOR_PRIMARY, .... Success, Failed
      timestamp: S, example: 2017-05-16T01:57:05.9Z
      ttl: one day
    Optional attr:
      instance_info: M, exmaple: author-primary: i-13ad9rxxxx
      last_command:  S, Last EC2 Run Command Id that trigggers this command,
                     used more for debugging
      externalId: S, provided by external parties, like Jenkins/Bamboo job id
    """

    # item ttl is set to 1 day
    ttl = (datetime.datetime.now() -
           datetime.datetime.fromtimestamp(0)).total_seconds()
    ttl += datetime.timedelta(days=1).total_seconds()

    item = {
        'command_id': {
            'S': command_id
        },
        'environment': {
            'S': environment
        },
        'state': {
            'S': state
        },
        'timestamp': {
            'S': timestamp
        },
        'ttl': {
            'N': str(ttl)
        }
    }

    if 'InstanceInfo' in kwargs and kwargs['InstanceInfo'] is not None:
        item['instance_info'] = {'M': kwargs['InstanceInfo']}

    if 'LastCommand' in kwargs and kwargs['LastCommand'] is not None:
        item['last_command'] = {'S': kwargs['LastCommand']}

    if 'ExternalId' in kwargs and kwargs['ExternalId'] is not None:
        item['externalId'] = {'S': kwargs['ExternalId']}

    dynamodb.put_item(
        TableName=table_name,
        Item=item
    )


# dynamodb is used to host state information
def get_state_from_dynamodb(table_name, command_id):

    item = dynamodb.get_item(
        TableName=table_name,
        Key={
            'command_id': {
                'S': command_id
            }
        },
        ConsistentRead=True,
        ReturnConsumedCapacity='NONE',
        ProjectionExpression='environment, #command_state, instance_info, externalId',
        ExpressionAttributeNames={
            '#command_state': 'state'
        }
    )

    return item


def update_state_in_dynamodb(table_name, command_id, new_state, timestamp):

    item_update = {
        'TableName': table_name,
        'Key': {
            'command_id': {
                'S': command_id
            }
        },
        'UpdateExpression': 'SET #S = :sval, #T = :tval',
        'ExpressionAttributeNames': {
            '#S': 'state',
            '#T': 'timestamp'
        },
        'ExpressionAttributeValues': {
            ':sval': {
                'S': new_state
            },
            ':tval': {
                'S': timestamp
            }
        }
    }

    dynamodb.update_item(**item_update)


# currently just forward the notification message from EC2 Run command.
def publish_status_message(topic, message):

    payload = {
        'default': message
    }

    sns.publish(
        TopicArn=topic,
        MessageStructure='json',
        Message=json.dumps(payload)
    )


def sns_message_processor(event, context):
    """
    offline snapshot is a complicated process, requiring a few things happen in the
    right order. This function will kick start the process. The bulk of operations
    will happen in another lambada function.
    """

    # reading in config info from either s3 or within bundle
    bucket = os.getenv('S3_BUCKET')
    prefix = os.getenv('S3_PREFIX')
    if bucket is not None and prefix is not None:
        config_file = '/tmp/config.json'
        s3.download_file(bucket, '{}/config.json'.format(prefix), config_file)
    else:
        logger.info('Unable to locate config.json in S3, searching within bundle')
        config_file = 'config.json'

    with open(config_file, 'r') as f:
        content = ''.join(f.readlines()).replace('\n', '')
        logger.debug('config file: ' + content)
        config = json.loads(content)

        run_command = config['ec2_run_command']
        task_document_mapping = config['document_mapping']
        offline_snapshot_config = config['offline_snapshot']

        dynamodb_table = run_command['dynamodb-table']
        status_topic_arn = run_command['status-topic-arn']

    ssm_common_params = {
        'TimeoutSeconds': 120,
        'OutputS3BucketName': run_command['cmd-output-bucket'],
        'OutputS3KeyPrefix': run_command['cmd-output-prefix'],
        'ServiceRoleArn': run_command['ssm-service-role-arn'],
        'NotificationConfig': {
            'NotificationArn': offline_snapshot_config['sns-topic-arn'],
            'NotificationEvents': [
                'Success',
                'Failed'
            ],
            'NotificationType': 'Command'
        }
    }

    for record in event['Records']:
        message_text = record['Sns']['Message']
        logger.debug(message_text)
        message = json.loads(message_text.replace('\'', '"'))

        # message that start-offline snapshot has a task key
        response = None
        if 'task' in message and message['task'] == 'offline-snapshot':

            stack_prefix = message['stack_prefix']
            external_id = None
            if 'externalId' in message:
                external_id = message['externalId']

            instances = stack_health_check(
                stack_prefix,
                offline_snapshot_config['min-publish-instances']
            )
            if instances is None:
                publish_status_message(
                    status_topic_arn,
                    json.dumps({
                        'status': 'Failed'
                    })
                )

                # if externalId is present, it means other parties are interested in the status.
                # Need to put a record in daynamodb with this id duplicated to command_id
                if 'externalId' is not None:
                    external_id = message['externalId']
                    put_state_in_dynamodb(
                        dynamodb_table, external_id, stack_prefix, 'Failed',
                        datetime.datetime.utcnow().isoformat()[:-3] + 'Z',
                        ExternalId=external_id
                    )

                raise Exception('Unhealthy Stack')

            ssm_params = ssm_common_params.copy()
            ssm_params.update(
                {
                    'InstanceIds': [instances['author-standby']],
                    'DocumentName': task_document_mapping['manage-service'],
                    'Comment': 'Kick start offline snapshot with stopping AEM service on Author standby instance',
                    'Parameters': {
                        'action': ['stop']
                    }
                }
            )

            response = send_ssm_cmd(ssm_params)

            instance_info = {key: {'S': value} for (key, value) in instances.items()}
            supplement = {
                'InstanceInfo': instance_info,
                'ExternalId': external_id
            }

            put_state_in_dynamodb(
                dynamodb_table,
                response['Command']['CommandId'],
                stack_prefix,
                'STOP_AUTHOR_STANDBY',
                datetime.datetime.utcnow().isoformat()[:-3] + 'Z',
                **supplement
            )
            return response
        else:
            cmd_id = message['commandId']

            if message['status'] == 'Failed':
                publish_status_message(
                    status_topic_arn,
                    json.dumps(message)
                    )

                update_state_in_dynamodb(dynamodb_table, cmd_id, 'Failed', message['eventTime'])
                raise Exception('Command {} failed.'.format(cmd_id))

            # get back the state of this task
            item = get_state_from_dynamodb(
                dynamodb_table,
                cmd_id
            )
            state = item['Item']['state']['S']
            stack_prefix = item['Item']['environment']['S']
            instance_info = item['Item']['instance_info']
            external_id = None
            if 'externalId' in item:
                external_id = item['Item']['externalId']['S']

            author_primary_id = instance_info['M']['author-primary']['S']
            author_standby_id = instance_info['M']['author-standby']['S']
            publish_id = instance_info['M']['publish']['S']

            if state == 'STOP_AUTHOR_STANDBY':
                ssm_params = ssm_common_params.copy()
                ssm_params.update(
                    {
                        'InstanceIds': [author_primary_id, publish_id],
                        'DocumentName': task_document_mapping['manage-service'],
                        'Comment': 'Stop AEM service on Author primary and Publish instances',
                        'Parameters': {
                            'action': ['stop']
                        }
                    }
                )

                response = send_ssm_cmd(ssm_params)
                put_state_in_dynamodb(
                    dynamodb_table,
                    response['Command']['CommandId'],
                    stack_prefix,
                    'STOP_AUTHOR_PRIMARY',
                    message['eventTime'],
                    ExternalId=external_id,
                    InstanceInfo=instance_info,
                    LastCommand=cmd_id
                )

            elif state == 'STOP_AUTHOR_PRIMARY':
                ssm_params = ssm_common_params.copy()
                ssm_params.update(
                    {
                        'InstanceIds': [author_primary_id, author_standby_id],
                        'DocumentName': task_document_mapping['offline-compaction'],
                        'Comment': 'Run offline compaction on Author primary and standby instances'
                    }
                )

                response = send_ssm_cmd(ssm_params)
                put_state_in_dynamodb(
                    dynamodb_table,
                    response['Command']['CommandId'],
                    stack_prefix,
                    'OFFLINE_COMPACTION',
                    message['eventTime'],
                    ExternalId=external_id,
                    InstanceInfo=instance_info,
                    LastCommand=cmd_id
                )

            elif state == 'OFFLINE_COMPACTION':
                ssm_params = ssm_common_params.copy()
                ssm_params.update(
                    {
                        'InstanceIds': [author_primary_id, publish_id],
                        'DocumentName': task_document_mapping['offline-snapshot'],
                        'Comment': 'Run offline EBS snapshot on Author primary and Publish instances'
                    }
                )

                response = send_ssm_cmd(ssm_params)
                put_state_in_dynamodb(
                    dynamodb_table,
                    response['Command']['CommandId'],
                    stack_prefix,
                    'OFFLINE_BACKUP',
                    message['eventTime'],
                    ExternalId=external_id,
                    InstanceInfo=instance_info,
                    LastCommand=cmd_id
                )

            elif state == 'OFFLINE_BACKUP':
                ssm_params = ssm_common_params.copy()
                ssm_params.update(
                    {
                        'InstanceIds': [author_primary_id],
                        'DocumentName': task_document_mapping['manage-service'],
                        'Comment': 'Start AEM service on Author primary instance',
                        'Parameters': {
                            'action': ['start']
                        }
                    }
                )

                response = send_ssm_cmd(ssm_params)
                put_state_in_dynamodb(
                    dynamodb_table,
                    response['Command']['CommandId'],
                    stack_prefix,
                    'START_AUTHOR_PRIMARY',
                    message['eventTime'],
                    ExternalId=external_id,
                    InstanceInfo=instance_info,
                    LastCommand=cmd_id
                )

            elif state == 'START_AUTHOR_PRIMARY':
                ssm_params = ssm_common_params.copy()
                ssm_params.update(
                    {
                        'InstanceIds': [author_standby_id, publish_id],
                        'DocumentName': task_document_mapping['manage-service'],
                        'Comment': 'Start AEM service on Author standby and Publish instances',
                        'Parameters': {
                            'action': ['start']
                        }
                    }
                )
                response = send_ssm_cmd(ssm_params)
                put_state_in_dynamodb(
                    dynamodb_table,
                    response['Command']['CommandId'],
                    stack_prefix,
                    'START_AUTHOR_STANDBY',
                    message['eventTime'],
                    ExternalId=external_id,
                    InstanceInfo=instance_info,
                    LastCommand=cmd_id
                )

            elif state == 'START_AUTHOR_STANDBY':
                publish_status_message(
                    status_topic_arn,
                    json.dumps(message)
                    )
                update_state_in_dynamodb(dynamodb_table, cmd_id, 'Success', message['eventTime'])
                print('Offline backup for environment {} finished successfully'.format(stack_prefix))

            else:
                raise Exception('Unexpected state {} for {}'.format(state, cmd_id))

            return response
