output "queue_url" {
  description = "URL of the SQS queue"
  value       = module.event_queue.queue_url
}

output "queue_arn" {
  description = "ARN of the SQS queue"
  value       = module.event_queue.queue_arn
}
