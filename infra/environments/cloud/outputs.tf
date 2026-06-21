output "queue_url" {
  description = "URL of the SQS queue"
  value       = module.event_queue.queue_url
}

output "queue_arn" {
  description = "ARN of the SQS queue"
  value       = module.event_queue.queue_arn
}

output "dlq_url" {
  description = "URL of the dead-letter queue"
  value       = module.event_queue.dlq_url
}

output "dlq_arn" {
  description = "ARN of the dead-letter queue"
  value       = module.event_queue.dlq_arn
}
