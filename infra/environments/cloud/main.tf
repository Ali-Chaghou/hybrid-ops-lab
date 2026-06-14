module "event_queue" {
  source = "../../modules/event-queue"

  queue_name = var.queue_name
}
