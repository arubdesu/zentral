import logging
import uuid
from zentral.core.events import event_cls_from_type, register_event_type
from zentral.core.events.base import BaseEvent, EventMetadata, EventRequest

logger = logging.getLogger('zentral.contrib.inventory.events')


ALL_EVENTS_SEARCH_DICT = {"tag": "machine"}


class EnrollmentSecretVerificationEvent(BaseEvent):
    event_type = 'enrollment_secret_verification'


register_event_type(EnrollmentSecretVerificationEvent)


# Inventory update events


class InventoryHeartbeat(BaseEvent):
    event_type = 'inventory_heartbeat'
    namespace = "inventory"
    tags = ['heartbeat', 'machine']


register_event_type(InventoryHeartbeat)


class AddMachine(BaseEvent):
    event_type = 'add_machine'
    namespace = "inventory"
    tags = ['machine']


register_event_type(AddMachine)


for attr in ('link',
             'business_unit',
             'group',
             'os_version',
             'system_info',
             'disk',
             'network_interface',
             'osx_app_instance',
             'deb_package',
             'program_instance',
             'teamviewer',
             'puppet_node',
             'principal_user',
             'certificate'):
    for action in ("add", "remove"):
        event_type = f"{action}_machine_{attr}"
        event_class_name = "".join(s.title() for s in event_type.split('_'))
        event_class = type(
            event_class_name,
            (BaseEvent,),
            {'event_type': event_type,
             'namespace': 'inventory',
             'tags': ['machine', 'machine_update']}
        )
        register_event_type(event_class)


def post_inventory_events(msn, events):
    event_uuid = uuid.uuid4()
    for index, (event_type, created_at, data) in enumerate(events):
        event_cls = event_cls_from_type(event_type)
        metadata = EventMetadata(machine_serial_number=msn,
                                 uuid=event_uuid, index=index,
                                 created_at=created_at)
        event = event_cls(metadata, data)
        event.post()


def post_enrollment_secret_verification_failure(model,
                                                user_agent, public_ip_address, serial_number,
                                                err_msg, enrollment_secret):
    event_cls = EnrollmentSecretVerificationEvent
    metadata = EventMetadata(machine_serial_number=serial_number,
                             request=EventRequest(user_agent, public_ip_address))
    payload = {"status": "failure",
               "reason": err_msg,
               "type": model}
    if enrollment_secret:
        obj = getattr(enrollment_secret, model)
        payload.update(obj.serialize_for_event())
    event = event_cls(metadata, payload)
    event.post()


def post_enrollment_secret_verification_success(request, model):
    obj = getattr(request.enrollment_secret, model)
    event_cls = EnrollmentSecretVerificationEvent
    metadata = EventMetadata(machine_serial_number=request.serial_number,
                             request=EventRequest(request.user_agent, request.public_ip_address))
    payload = {"status": "success",
               "type": model}
    payload.update(obj.serialize_for_event())
    event = event_cls(metadata, payload)
    event.post()
