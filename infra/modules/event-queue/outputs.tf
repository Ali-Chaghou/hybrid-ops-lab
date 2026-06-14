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
