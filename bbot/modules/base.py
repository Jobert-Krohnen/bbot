import queue
import logging
import threading
import traceback
from time import sleep
from contextlib import suppress

from ..core.helpers.threadpool import ThreadPoolWrapper
from ..core.errors import ScanCancelledError, ValidationError, WordlistError

from bbot.core.event.base import is_event


class BaseModule:

    # Event types to watch
    watched_events = []
    # Event types to produce
    produced_events = []
    # Module description, etc.
    meta = {"auth_required": False, "description": "Base module"}
    # Flags, must include either "passive" or "active"
    flags = []

    # python dependencies (pip install ____)
    deps_pip = []
    # apt dependencies (apt install ____)
    deps_apt = []
    # other dependences as shell commands
    # uses ansible.builtin.shell (https://docs.ansible.com/ansible/latest/collections/ansible/builtin/shell_module.html)
    deps_shell = []
    # list of ansible tasks for when other dependency installation methods aren't enough
    deps_ansible = []
    # Whether to accept incoming duplicate events
    accept_dupes = False
    # Whether to block outgoing duplicate events
    suppress_dupes = True

    # Scope distance modifier - accept/deny events based on scope distance
    # None == accept all events
    # 2 == accept events up to and including the scan's configured search distance plus two
    # 1 == accept events up to and including the scan's configured search distance plus one
    # 0 == accept events up to and including the scan's configured search distance
    # -1 == accept events up to and including the scan's configured search distance minus one
    #       (this is the default setting because when the scan's configured search distance == 1
    #       [the default], then this is equivalent to in_scope_only)
    # -2 == accept events up to and including the scan's configured search distance minus two
    scope_distance_modifier = -1
    # Only accept the initial target event(s)
    target_only = False
    # Only accept explicitly in-scope events (scope distance == 0)
    # Use this options if your module is aggressive or if you don't want it to scale with
    #   the scan's search distance
    in_scope_only = False

    # Options, e.g. {"api_key": ""}
    options = {}
    # Options description, e.g. {"api_key": "API Key"}
    options_desc = {}
    # Maximum concurrent instances of handle_event() or handle_batch()
    max_event_handlers = 1
    # Max number of concurrent calls to submit_task()
    max_threads = 10
    # Batch size
    # If batch size > 1, override handle_batch() instead of handle_event()
    batch_size = 1
    # Seconds to wait before force-submitting batch
    batch_wait = 10
    # When set to false, prevents events generated by this module from being automatically marked as in-scope
    # Useful for low-confidence modules like speculate and ipneighbor
    _scope_shepherding = True
    # Exclude from scan statistics
    _stats_exclude = False
    # outgoing queue size
    _qsize = 100
    # Priority of events raised by this module, 1-5, lower numbers == higher priority
    _priority = 3
    # Name, overridden automatically
    _name = "base"
    # Type, for differentiating between normal modules and output modules, etc.
    _type = "scan"

    def __init__(self, scan):
        self.scan = scan
        self.errored = False
        self._log = None
        self._event_queue = None
        # this semaphore allows us to track how many events are waiting to go out
        # so that a single module can't raise too many events at once
        self._event_semaphore = threading.Semaphore(self._qsize)
        # how many seconds we've gone without processing a batch
        self._batch_idle = 0
        # wrapper around shared thread pool to ensure that a single module doesn't hog more than its share
        self.thread_pool = ThreadPoolWrapper(
            self.scan._thread_pool.executor, max_workers=self.config.get("max_threads", self.max_threads)
        )
        self._internal_thread_pool = ThreadPoolWrapper(
            self.scan._internal_thread_pool.executor, max_workers=self.max_event_handlers
        )
        # additional callbacks to be executed alongside self.cleanup()
        self.cleanup_callbacks = []
        self._cleanedup = False
        self._watched_events = None

    def setup(self):
        """
        Perform setup functions at the beginning of the scan.
        Optionally override this method.

        Must return True or False based on whether the setup was successful
        """
        return True

    def handle_event(self, event):
        """
        Override this method if batch_size == 1.
        """
        pass

    def handle_batch(self, *events):
        """
        Override this method if batch_size > 1.
        """
        pass

    def filter_event(self, event):
        """
        Accept/reject events based on custom criteria

        Override this method if you need more granular control
        over which events are distributed to your module
        """
        return True

    def finish(self):
        """
        Perform final functions when scan is nearing completion

        For example,  if your module relies on the word cloud, you may choose to wait until
        the scan is finished (and the word cloud is most complete) before running an operation.

        Note that this method may be called multiple times, because it may raise events.
        Optionally override this method.
        """
        return

    def report(self):
        """
        Perform a final task when the scan is finished, but before cleanup happens

        This is useful for modules that aggregate data and raise summary events at the end of a scan
        """
        return

    def cleanup(self):
        """
        Perform final cleanup after the scan has finished
        This method is called only once, and may not raise events.
        Optionally override this method.
        """
        return

    def get_watched_events(self):
        """
        Override if you need your watched_events to be dynamic
        """
        if self._watched_events is None:
            self._watched_events = set(self.watched_events)
        return self._watched_events

    def submit_task(self, *args, **kwargs):
        return self.thread_pool.submit_task(self.catch, *args, **kwargs)

    def catch(self, *args, **kwargs):
        return self.scan.manager.catch(*args, **kwargs)

    def _handle_batch(self, force=False):
        if self.num_queued_events > 0 and (force or self.num_queued_events >= self.batch_size):
            self._batch_idle = 0
            on_finish_callback = None
            events, finish, report = self.events_waiting
            if finish:
                on_finish_callback = self.finish
            elif report:
                on_finish_callback = self.report
            if events:
                self.debug(f"Handling batch of {len(events):,} events")
                self._internal_thread_pool.submit_task(
                    self.catch,
                    self.handle_batch,
                    *events,
                    _on_finish_callback=on_finish_callback,
                )
                return True
        return False

    def make_event(self, *args, **kwargs):
        raise_error = kwargs.pop("raise_error", False)
        try:
            event = self.scan.make_event(*args, **kwargs)
        except ValidationError as e:
            if raise_error:
                raise
            self.warning(f"{e}")
            return
        if not event.module:
            event.module = self
        return event

    def emit_event(self, *args, **kwargs):
        if self.scan.stopping:
            return
        on_success_callback = kwargs.pop("on_success_callback", None)
        abort_if = kwargs.pop("abort_if", lambda e: False)
        quick = kwargs.pop("quick", False)
        event = self.make_event(*args, **kwargs)
        if event:
            while not self.scan.stopping:
                okay = event.acquire_semaphore(timeout=0.1)
                if not okay:
                    continue
                try:
                    self.scan.manager.emit_event(
                        event,
                        abort_if=abort_if,
                        on_success_callback=on_success_callback,
                        quick=quick,
                    )
                except Exception:
                    event.release_semaphore()
                    self.error(f"Unexpected error in {self.name}.emit_event()")
                    self.debug(traceback.format_exc())
                break

    @property
    def events_waiting(self):
        """
        yields all events in queue, up to maximum batch size
        """
        events = []
        finish = False
        report = False
        left = int(self.batch_size)
        while left > 0 and self.event_queue:
            try:
                event = self.event_queue.get_nowait()
                if type(event) == str:
                    if event == "FINISHED":
                        finish = True
                    elif event == "REPORT":
                        report = True
                else:
                    left -= 1
                    events.append(event)
            except queue.Empty:
                break
        return events, finish, report

    @property
    def num_queued_events(self):
        ret = 0
        if self.event_queue:
            ret = self.event_queue.qsize()
        return ret

    def start(self):
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _setup(self):

        status_codes = {False: "hard-fail", None: "soft-fail", True: "success"}

        status = False
        self.debug(f"Setting up module {self.name}")
        try:
            result = self.setup()
            if type(result) == tuple and len(result) == 2:
                status, msg = result
            else:
                status = result
                msg = status_codes[status]
            self.debug(f"Finished setting up module {self.name}")
        except Exception as e:
            self.set_error_state()
            if isinstance(e, WordlistError):
                status = None
            msg = f"{e}"
            self.debug(traceback.format_exc())
        return status, str(msg)

    @property
    def _force_batch(self):
        """
        Determine whether a batch should be forcefully submitted
        """
        # if we've been idle long enough
        if self._batch_idle >= self.batch_wait:
            return True
        # if scan is finishing
        if self.scan.status == "FINISHING":
            return True
        # if there's a batch stalemate
        batch_modules = [m for m in self.scan.modules.values() if m.batch_size > 1]
        if all([(not m.running) for m in batch_modules]):
            return True
        return False

    def _worker(self):
        # keep track of how long we've been running
        iterations = 0
        try:
            while not self.scan.stopping:
                iterations += 1
                if self.batch_size > 1:
                    if iterations % 10 == 0:
                        self._batch_idle += 1
                    force = self._force_batch
                    if force:
                        self._batch_idle = 0
                    submitted = self._handle_batch(force=force)
                    if not submitted:
                        sleep(0.1)

                else:
                    try:
                        if self.event_queue:
                            e = self.event_queue.get(timeout=0.1)
                        else:
                            self.debug(f"Event queue is in bad state")
                            return
                    except queue.Empty:
                        continue
                    self.debug(f"Got {e} from {getattr(e, 'module', e)}")
                    # if we receive the special "FINISHED" event
                    if type(e) == str:
                        if e == "FINISHED":
                            self._internal_thread_pool.submit_task(self.catch, self.finish)
                        elif e == "REPORT":
                            self._internal_thread_pool.submit_task(self.catch, self.report)
                    else:
                        if self._type == "output":
                            self.catch(self.handle_event, e)
                        else:
                            self._internal_thread_pool.submit_task(self.catch, self.handle_event, e)

        except KeyboardInterrupt:
            self.debug(f"Interrupted")
            self.scan.stop()
        except ScanCancelledError as e:
            self.verbose(f"Scan cancelled, {e}")
        except Exception as e:
            self.set_error_state(f"Exception ({e.__class__.__name__}) in module {self.name}:\n{e}")
            self.debug(traceback.format_exc())

    def _filter_event(self, event, precheck_only=False):
        acceptable, reason = self._event_precheck(event)
        if acceptable and not precheck_only:
            acceptable, reason = self._event_postcheck(event)
        return acceptable, reason

    @property
    def max_scope_distance(self):
        if self.in_scope_only or self.target_only:
            return 0
        return max(0, self.scan.scope_search_distance + self.scope_distance_modifier)

    def _event_precheck(self, event):
        """
        Check if an event should be accepted by the module
        These checks are safe to run before an event has been DNS-resolved
        """
        # special "FINISHED" event
        if type(event) == str:
            if event in ("FINISHED", "REPORT"):
                return True, ""
            else:
                return False, f'string value "{event}" is invalid'
        # exclude non-watched types
        if not any(t in self.get_watched_events() for t in ("*", event.type)):
            return False, "its type is not in watched_events"
        if self.target_only:
            if "target" not in event.tags:
                return False, "it did not meet target_only filter criteria"
        # if event is an IP address that was speculated from a CIDR
        source_is_range = getattr(event.source, "type", "") == "IP_RANGE"
        if (
            source_is_range
            and event.type == "IP_ADDRESS"
            and str(event.module) == "speculate"
            and self.name != "speculate"
        ):
            # and the current module listens for both ranges and CIDRs
            if all([x in self.watched_events for x in ("IP_RANGE", "IP_ADDRESS")]):
                # then skip the event.
                # this helps avoid double-portscanning both an individual IP and its parent CIDR.
                return False, "module consumes IP ranges directly"
        return True, ""

    def _event_postcheck(self, event):
        """
        Check if an event should be accepted by the module
        These checks must be run after an event has been DNS-resolved
        """
        if type(event) == str:
            return True, ""

        if self.in_scope_only:
            if event.scope_distance > 0:
                return False, "it did not meet in_scope_only filter criteria"
        if self.scope_distance_modifier is not None:
            if event.scope_distance < 0:
                return False, f"its scope_distance ({event.scope_distance}) is invalid."
            elif event.scope_distance > self.max_scope_distance:
                return (
                    False,
                    f"its scope_distance ({event.scope_distance}) exceeds the maximum allowed by the scan ({self.scan.scope_search_distance}) + the module ({self.scope_distance_modifier}) == {self.max_scope_distance}",
                )

        # custom filtering
        try:
            if not self.filter_event(event):
                return False, f"{event} did not meet custom filter criteria"
        except Exception as e:
            import traceback

            self.error(f"Error in filter_event({event}): {e}")
            self.debug(traceback.format_exc())

        return True, ""

    def _cleanup(self):
        if not self._cleanedup:
            self._cleanedup = True
            for callback in [self.cleanup] + self.cleanup_callbacks:
                if callable(callback):
                    self.catch(callback, _force=True)

    def queue_event(self, event):
        if self.event_queue is not None and not self.errored:
            acceptable, reason = self._filter_event(event)
            if not acceptable and reason:
                self.debug(f"Not accepting {event} because {reason}")
                return
            if is_event(event):
                self.scan.stats.event_consumed(event, self)
            self.event_queue.put(event)
        else:
            self.debug(f"Not in an acceptable state to queue event")

    def set_error_state(self, message=None):
        if message is not None:
            self.error(str(message))
        if not self.errored:
            self.debug(f"Setting error state for module {self.name}")
            self.errored = True
            # clear incoming queue
            if self.event_queue:
                self.debug(f"Emptying event_queue")
                with suppress(queue.Empty):
                    while 1:
                        self.event_queue.get_nowait()
                # set queue to None to prevent its use
                # if there are leftover objects in the queue, the scan will hang.
                self._event_queue = False

    @property
    def name(self):
        return str(self._name)

    @property
    def helpers(self):
        return self.scan.helpers

    @property
    def status(self):
        main_pool = self.thread_pool.num_tasks
        internal_pool = self._internal_thread_pool.num_tasks
        pool_total = main_pool + internal_pool
        incoming_qsize = 0
        if self.event_queue:
            incoming_qsize = self.event_queue.qsize()
        outgoing_qsize = self._qsize - self._event_semaphore._value
        status = {
            "events": {"incoming": incoming_qsize, "outgoing": outgoing_qsize},
            "tasks": {"main_pool": main_pool, "internal_pool": internal_pool, "total": pool_total},
            "errored": self.errored,
        }
        status["running"] = self._is_running(status)
        return status

    @staticmethod
    def _is_running(module_status):
        for pool, count in module_status["tasks"].items():
            if count > 0:
                return True
        return False

    @property
    def running(self):
        """
        Indicates whether the module is currently processing data.
        """
        return self._is_running(self.status)

    @property
    def config(self):
        config = self.scan.config.get("modules", {}).get(self.name, {})
        if config is None:
            config = {}
        return config

    @property
    def event_queue(self):
        if self._event_queue is None:
            self._event_queue = queue.SimpleQueue()
        return self._event_queue

    @property
    def priority(self):
        return int(max(1, min(5, self._priority)))

    @property
    def auth_required(self):
        return self.meta.get("auth_required", False)

    @property
    def log(self):
        if self._log is None:
            self._log = logging.getLogger(f"bbot.modules.{self.name}")
        return self._log

    def __str__(self):
        return self.name

    def stdout(self, *args, **kwargs):
        self.log.stdout(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def debug(self, *args, **kwargs):
        self.log.debug(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def verbose(self, *args, **kwargs):
        self.log.verbose(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugeverbose(self, *args, **kwargs):
        self.log.hugeverbose(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def info(self, *args, **kwargs):
        self.log.info(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugeinfo(self, *args, **kwargs):
        self.log.hugeinfo(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def success(self, *args, **kwargs):
        self.log.success(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugesuccess(self, *args, **kwargs):
        self.log.hugesuccess(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def warning(self, *args, **kwargs):
        self.log.warning(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugewarning(self, *args, **kwargs):
        self.log.hugewarning(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def error(self, *args, **kwargs):
        self.log.error(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def critical(self, *args, **kwargs):
        self.log.critical(*args, extra={"scan_id": self.scan.id}, **kwargs)
