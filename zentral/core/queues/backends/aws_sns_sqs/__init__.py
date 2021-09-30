from importlib import import_module
import json
import logging
import queue
import signal
import sys
import threading
import time
import boto3
from botocore.config import Config
from django.utils.functional import cached_property
from django.utils.text import slugify
from zentral.conf import settings
from .consumer import BatchConsumer, ConcurrentConsumer, Consumer, ConsumerProducer
from .sns import SNSPublishThread
from .sqs import SQSSendThread


logger = logging.getLogger('zentral.core.queues.backends.aws_sns_sqs')


# SNS/SQS setup
#
# SQS Q raw-events-queue
# → SQS Q events-queue
#   → SNS T enriched-events-topic (fanout)
#     → SQS Q process-enriched-events-queue
#     → SQS Q store-enriched-events-*-queue


class WorkerMixin:
    name = "UNDEFINED"
    counters = []

    def setup_metrics_exporter(self, *args, **kwargs):
        self.metrics_exporter = kwargs.pop("metrics_exporter", None)
        if self.metrics_exporter:
            for name, label in self.counters:
                self.metrics_exporter.add_counter(name, [label])
            self.metrics_exporter.start()

    def inc_counter(self, name, label):
        if self.metrics_exporter:
            self.metrics_exporter.inc(name, label)

    def log(self, msg, level, *args):
        logger.log(level, "{} - {}".format(self.name, msg), *args)

    def log_debug(self, msg, *args):
        self.log(msg, logging.DEBUG, *args)

    def log_info(self, msg, *args):
        self.log(msg, logging.INFO, *args)

    def log_error(self, msg, *args):
        self.log(msg, logging.ERROR, *args)


class PreprocessWorker(WorkerMixin, ConsumerProducer):
    name = "preprocess worker"
    counters = (
        ("preprocessed_events", "routing_key"),
        ("produced_events", "event_type"),
    )

    def __init__(self, event_queues):
        super().__init__(event_queues.setup_queue("raw-events"), event_queues.client_kwargs)
        self._threads.append(
            SQSSendThread(
                event_queues.setup_queue("events"),
                self.stop_event,
                self.publish_message_queue,
                self.published_message_queue,
                event_queues.client_kwargs
            )
        )
        # preprocessors
        self.preprocessors = {
            preprocessor.routing_key: preprocessor
            for preprocessor in self._get_preprocessors()
        }

    def _get_preprocessors(self):
        for app in settings['apps']:
            try:
                preprocessors_module = import_module("{}.preprocessors".format(app))
            except ImportError:
                pass
            else:
                yield from getattr(preprocessors_module, "get_preprocessors")()

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def generate_events(self, routing_key, event_d):
        if not routing_key:
            logger.error("Message w/o routing key")
        else:
            preprocessor = self.preprocessors.get(routing_key)
            if not preprocessor:
                logger.error("No preprocessor for routing key %s", routing_key)
            else:
                for event in preprocessor.process_raw_event(event_d):
                    yield None, event.serialize(machine_metadata=False)
                    self.inc_counter("produced_events", event.event_type)
        self.inc_counter("preprocessed_events", routing_key or "UNKNOWN")


class EnrichWorker(WorkerMixin, ConsumerProducer):
    name = "enrich worker"
    counters = (
        ("enriched_events", "event_type"),
        ("produced_events", "event_type"),
    )
    publish_thread_number = 10

    def __init__(self, event_queues, enrich_event):
        super().__init__(event_queues.setup_queue("events"), event_queues.client_kwargs)
        for thread_id in range(self.publish_thread_number):
            self._threads.append(
                SNSPublishThread(
                    thread_id,
                    event_queues.setup_topic("enriched-events"),
                    self.stop_event,
                    self.publish_message_queue,
                    self.published_message_queue,
                    event_queues.client_kwargs
                )
            )
        self._enrich_event = enrich_event

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def generate_events(self, routing_key, event_d):
        self.log_debug("enrich event")
        for event in self._enrich_event(event_d):
            yield None, event.serialize(machine_metadata=True)
            self.inc_counter("produced_events", event.event_type)
        self.inc_counter("enriched_events", event.event_type)


class ProcessWorker(WorkerMixin, Consumer):
    name = "process worker"
    counters = (
        ("processed_events", "event_type"),
    )

    def __init__(self, event_queues, process_event):
        super().__init__(
            event_queues.setup_queue(
                "process-enriched-events",
                "enriched-events"
            ),
            event_queues.client_kwargs
        )
        self._process_event = process_event

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def process_event(self, routing_key, event_d):
        self.log_debug("process event")
        event_type = event_d['_zentral']['type']
        self._process_event(event_d)
        self.inc_counter("processed_events", event_type)


class SimpleStoreWorker(WorkerMixin, Consumer):
    counters = (
        ("stored_events", "event_type"),
    )

    def __init__(self, event_queues, event_store):
        super().__init__(
            event_queues.setup_queue(
                "store-enriched-events-{}".format(slugify(event_store.name)),
                "enriched-events"
            ),
            event_queues.client_kwargs
        )
        self.event_store = event_store
        self.name = "store worker {}".format(self.event_store.name)

    def skip_event(self, receipt_handle, event_d):
        event_type = event_d['_zentral']['type']
        return not self.event_store.is_event_type_included(event_type)

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def process_event(self, routing_key, event_d):
        self.log_debug("store event")
        event_type = event_d['_zentral']['type']
        self.event_store.store(event_d)
        self.inc_counter("stored_events", event_type)


class ConcurrentStoreWorker(WorkerMixin, ConcurrentConsumer):
    counters = (
        ("stored_events", "event_type"),
    )

    def __init__(self, event_queues, event_store):
        self.event_store = event_store
        super().__init__(
            event_queues.setup_queue(
                "store-enriched-events-{}".format(slugify(event_store.name)),
                "enriched-events"
            ),
            event_store.concurrency,
            event_queues.client_kwargs
        )
        self.name = f"store worker {event_store.name}"

    def skip_event(self, receipt_handle, event_d):
        event_type = event_d['_zentral']['type']
        return not self.event_store.is_event_type_included(event_type)

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def get_process_thread_constructor(self):
        return self.event_store.get_process_thread_constructor()

    def update_metrics(self, success, event_type, process_time):
        if success:
            self.inc_counter("stored_events", event_type)


class BulkStoreWorker(WorkerMixin, BatchConsumer):
    counters = (
        ("stored_events", "event_type"),
    )

    def __init__(self, event_queues, event_store):
        super().__init__(
            event_queues.setup_queue(
                "store-enriched-events-{}".format(slugify(event_store.name)),
                "enriched-events"
            ),
            event_store.batch_size,
            event_queues.client_kwargs
        )
        self.event_store = event_store
        self.name = "store worker {}".format(self.event_store.name)

    def skip_event(self, receipt_handle, event_d):
        event_type = event_d['_zentral']['type']
        return not self.event_store.is_event_type_included(event_type)

    def run(self, *args, **kwargs):
        self.log_info("run")
        super().setup_metrics_exporter(*args, **kwargs)
        super().run(*args, **kwargs)

    def process_events(self, batch):
        batch_size = len(batch)
        self.log_debug("store %d events", batch_size)
        self.event_info = {}

        def iter_events():
            while batch:
                # the routing key is ignored
                receipt_handle, _, event_d = batch.popleft()
                # WARN: This is a simple implementation where there is one receipt_handle per event_d
                event_metadata = event_d['_zentral']
                event_key = (event_metadata["id"], event_metadata["index"])
                event_type = event_metadata['type']
                self.event_info[event_key] = (receipt_handle, event_type)
                yield event_d

        stored_event_count = 0
        for stored_event_key in self.event_store.bulk_store(iter_events()):
            try:
                receipt_handle, event_type = self.event_info[stored_event_key]
            except KeyError:
                logger.error("unknown stored event %s", stored_event_key)
            else:
                yield receipt_handle
                self.inc_counter("stored_events", event_type)
                stored_event_count += 1

        if stored_event_count < batch_size:
            self.log_error("only %s/%s event(s) stored", stored_event_count, batch_size)
        else:
            self.log_debug("%s/%s events stored", stored_event_count, batch_size)


class EventQueues(object):
    def __init__(self, config_d):
        self._prefix = config_d.get("prefix", "ztl-")
        self._tags = config_d.get("tags", {"Product": "Zentral"})
        self.client_kwargs = {
            "config": Config(
                retries={
                    "max_attempts": 3,
                    "mode": "standard"
                }
            )
        }
        for kwarg in ("region_name",
                      "endpoint_url",
                      "aws_access_key_id",
                      "aws_secret_access_key",
                      "aws_session_token"):
            val = config_d.get(kwarg)
            if val:
                self.client_kwargs[kwarg] = val
        self._known_queues = {}
        for queue_basename, queue_url in config_d.get("predefined_queues", {}).items():
            self._known_queues[queue_basename] = queue_url
        self._known_topics = {}
        for topic_basename, topic_arn in config_d.get("predefined_topics", {}).items():
            self._known_topics[topic_basename] = topic_arn
        self._raw_events_queue = None
        self._events_queue = None
        self._stop_event = None
        self._threads = []

    @cached_property
    def sns_client(self):
        return boto3.client("sns", **self.client_kwargs)

    @cached_property
    def sqs_client(self):
        return boto3.client("sqs", **self.client_kwargs)

    def setup_topic(self, topic_basename):
        try:
            return self._known_topics[topic_basename]
        except KeyError:
            topic_name = "{}{}-topic".format(self._prefix, topic_basename)
            response = self.sns_client.create_topic(
                Name=topic_name,
                Tags=[{"Key": k, "Value": v} for k, v in self._tags.items()]
            )
            topic_arn = response["TopicArn"]
            self._known_topics[topic_basename] = topic_arn
            return topic_arn

    def setup_queue(self, queue_basename, topic_basename=None):
        try:
            return self._known_queues[queue_basename]
        except KeyError:
            queue_name = "{}{}-queue".format(self._prefix, queue_basename)
            try:
                response = self.sqs_client.get_queue_url(QueueName=queue_name)
            except self.sqs_client.exceptions.QueueDoesNotExist:
                response = self.sqs_client.create_queue(
                    QueueName=queue_name,
                    tags=self._tags,
                )
            queue_url = response["QueueUrl"]
            self._known_queues[queue_basename] = queue_url
            if topic_basename:
                topic_arn = self.setup_topic(topic_basename)
                response = self.sqs_client.get_queue_attributes(
                    QueueUrl=queue_url,
                    AttributeNames=["QueueArn"]
                )
                queue_arn = response["Attributes"]["QueueArn"]
                self.sqs_client.set_queue_attributes(
                    QueueUrl=queue_url,
                    Attributes={
                        "Policy": json.dumps({
                            "Version": "2012-10-17",
                            "Statement": [
                                {"Sid": "AllowSendMessageFromSNSTopic",
                                 "Principal": {"Service": "sns.amazonaws.com"},
                                 "Action": ["sqs:SendMessage"],
                                 "Effect": "Allow",
                                 "Resource": queue_arn,
                                 "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}}}
                            ]
                        })
                    }
                )
                self.sns_client.subscribe(
                    TopicArn=topic_arn,
                    Protocol="sqs",
                    Endpoint=queue_arn,
                    Attributes={"RawMessageDelivery": "true"},
                )
            return queue_url

    def get_preprocess_worker(self):
        return PreprocessWorker(self)

    def get_enrich_worker(self, enrich_event):
        return EnrichWorker(self, enrich_event)

    def get_process_worker(self, process_event):
        return ProcessWorker(self, process_event)

    def get_store_worker(self, event_store):
        if event_store.batch_size > 1:
            return BulkStoreWorker(self, event_store)
        elif event_store.concurrency > 1:
            return ConcurrentStoreWorker(self, event_store)
        else:
            return SimpleStoreWorker(self, event_store)

    def _setup_signal(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._handle_sigterm)

    def _graceful_stop(self, signum, frame):
        if signum == signal.SIGTERM:
            signum = "SIGTERM"
        elif signum == signal.SIGINT:
            signum = "SIGINT"
        logger.debug("Received signal %s", signum)
        if not self._stop_event.is_set():
            logger.error("Signal %s. Initiate graceful stop.", signum)
            self._stop_event.set()
            for thread in self._threads:
                thread.join()
            logger.error("All threads stopped. Exit 0.")
            sys.exit(0)

    def _setup_graceful_stop(self):
        if self._stop_event is None:
            self._stop_event = threading.Event()
            if threading.current_thread() is threading.main_thread():
                logger.debug("setup graceful stop")
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, self._graceful_stop)
            else:
                logger.warning("could not setup graceful stop: not running on main thread")

    def post_raw_event(self, routing_key, raw_event):
        self._setup_graceful_stop()
        if self._raw_events_queue is None:
            self._raw_events_queue = queue.Queue(maxsize=20)
            thread = SQSSendThread(
                self.setup_queue("raw-events"),
                self._stop_event,
                self._raw_events_queue,
                None,
                self.client_kwargs
            )
            thread.start()
            self._threads.append(thread)
        self._raw_events_queue.put((None, routing_key, raw_event, time.monotonic()))

    def post_event(self, event):
        self._setup_graceful_stop()
        if self._events_queue is None:
            self._events_queue = queue.Queue(maxsize=20)
            thread = SQSSendThread(
                self.setup_queue("events"),
                self._stop_event,
                self._events_queue,
                None,
                self.client_kwargs
            )
            thread.start()
            self._threads.append(thread)
        self._events_queue.put((None, None, event.serialize(machine_metadata=False), time.monotonic()))
