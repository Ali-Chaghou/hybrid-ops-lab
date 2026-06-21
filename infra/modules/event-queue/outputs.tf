output "queue_url" {
  description = "URL of the created SQS queue"
  value       = aws_sqs_queue.this.url
}

output "queue_arn" {
  description = "ARN of the created SQS queue"
  value       = aws_sqs_queue.this.arn
}

output "queue_name" {
  description = "Name of the created SQS queue"
  value       = aws_sqs_queue.this.name
}

output "dlq_url" {
  description = "URL of the dead-letter queue"
  value       = aws_sqs_queue.dlq.url
}

output "dlq_arn" {
  description = "ARN of the dead-letter queue"
  value       = aws_sqs_queue.dlq.arn
}

output "dlq_name" {
  description = "Name of the dead-letter queue"
  value       = aws_sqs_queue.dlq.name
}

output "max_receive_count" {
  description = "maxReceiveCount used in the main queue redrive policy"
  value       = var.max_receive_count
}
