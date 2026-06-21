# event-queue

Wiederverwendbares OpenTofu-Modul für eine SQS-Queue. AWS-portabel; Visibility-Timeout
und Retention sind parametrierbar, Server-Side-Encryption (SSE-SQS) ist standardmäßig
aktiv. Zur lokalen Verwendung gegen ElasticMQ siehe
[`../../environments/cloud`](../../environments/cloud) und
[ADR-005](../../../docs/decisions/005-elasticmq-statt-localstack.md).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.12.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.50.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_sqs_queue.dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |
| [aws_sqs_queue.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |
| [aws_sqs_queue_redrive_allow_policy.dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue_redrive_allow_policy) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_dlq_message_retention_seconds"></a> [dlq\_message\_retention\_seconds](#input\_dlq\_message\_retention\_seconds) | Retention for the dead-letter queue. Default 14 days (AWS maximum) so poison messages can be inspected/replayed long after they arrive. | `number` | `1209600` | no |
| <a name="input_max_receive_count"></a> [max\_receive\_count](#input\_max\_receive\_count) | Number of times a message may be received before SQS redrives it to the DLQ.<br/>Chosen at 5: high enough that a short transient failure (DB blip / brief queue<br/>hiccup) does not immediately dead-letter a message, low enough that a truly<br/>unprocessable (poison) message leaves the main queue after a bounded number of<br/>visibility cycles instead of being redelivered forever. | `number` | `5` | no |
| <a name="input_message_retention_seconds"></a> [message\_retention\_seconds](#input\_message\_retention\_seconds) | How long messages are retained in the queue | `number` | `345600` | no |
| <a name="input_queue_name"></a> [queue\_name](#input\_queue\_name) | Name of the SQS queue | `string` | n/a | yes |
| <a name="input_sqs_managed_sse_enabled"></a> [sqs\_managed\_sse\_enabled](#input\_sqs\_managed\_sse\_enabled) | Enable server-side encryption with SQS-managed keys (SSE-SQS) | `bool` | `true` | no |
| <a name="input_visibility_timeout_seconds"></a> [visibility\_timeout\_seconds](#input\_visibility\_timeout\_seconds) | Time a message stays hidden after being received | `number` | `30` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_dlq_arn"></a> [dlq\_arn](#output\_dlq\_arn) | ARN of the dead-letter queue |
| <a name="output_dlq_name"></a> [dlq\_name](#output\_dlq\_name) | Name of the dead-letter queue |
| <a name="output_dlq_url"></a> [dlq\_url](#output\_dlq\_url) | URL of the dead-letter queue |
| <a name="output_max_receive_count"></a> [max\_receive\_count](#output\_max\_receive\_count) | maxReceiveCount used in the main queue redrive policy |
| <a name="output_queue_arn"></a> [queue\_arn](#output\_queue\_arn) | ARN of the created SQS queue |
| <a name="output_queue_name"></a> [queue\_name](#output\_queue\_name) | Name of the created SQS queue |
| <a name="output_queue_url"></a> [queue\_url](#output\_queue\_url) | URL of the created SQS queue |
<!-- END_TF_DOCS -->
