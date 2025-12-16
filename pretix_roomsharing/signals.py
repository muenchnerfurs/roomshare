# Register your receivers here
import logging
from django import forms
from django.db import transaction
from django.db.models import QuerySet
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Event, Order, OrderPosition, CartPosition
from pretix.base.pdf import get_variables
from pretix.base.services.orders import OrderError
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import logentry_display, order_placed, order_canceled, validate_order, layout_text_variables
from pretix.control.forms.filter import FilterForm
from pretix.control.signals import (
    nav_event,
    nav_event_settings,
    order_info as control_order_info,
)
from pretix.presale.signals import (
    checkout_confirm_page_content,
    checkout_flow_steps,
    order_info,
    order_meta_from_request,
)
from pretix.presale.views import get_cart
from pretix.presale.views.cart import cart_session
from pretix.base.signals import register_data_exporters

from .checkoutflow import RoomStep
from .models import OrderRoom, Room
from .exporter import RoomExporter

logger = logging.getLogger(__name__)


@receiver(signal=checkout_flow_steps, dispatch_uid="room_checkout_step")
def signal_checkout_flow_steps(sender, **kwargs):
    return RoomStep


@receiver(nav_event_settings, dispatch_uid="pretix_roomsharing")
def navbar_settings(sender, request, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("Roomsharing"),
            "url": reverse(
                "plugins:pretix_roomsharing:control.room.settings",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:pretix_roomsharing"
            and url.url_name == "control.room.settings",
        }
    ]


@receiver(order_meta_from_request, dispatch_uid="room_order_meta")
def order_meta_signal(sender: Event, request: HttpRequest, **kwargs):
    cs = cart_session(request)
    return {
        "room_mode": cs.get("room_mode"),
        "room_join": cs.get("room_join"),
        "room_create": cs.get("room_create"),
    }


@receiver(order_placed, dispatch_uid="room_order_placed")
def placed_order(sender: Event, order: Order, **kwargs):
    if order.meta_info_data and order.meta_info_data.get("room_mode") in ["create", "join"]:
        try:
            order_room = OrderRoom.objects.get(pk=order.meta_info_data["room_join"])
        except OrderRoom.DoesNotExist:
            logger.error("OrderRoom did not exist, can't update OrderRoom")
            return
        else:
            order_room.order = order
            order_room.cart_id = None
            order_room.save()


@receiver(order_canceled, dispatch_uid="room_order_canceled")
def cancel_order(sender: Event, order: Order, **kwargs):
    try:
        OrderRoom.objects.get(order=order).delete()
    except OrderRoom.DoesNotExist:
        pass


@receiver(checkout_confirm_page_content, dispatch_uid="room_confirm")
def confirm_page(sender: Event, request: HttpRequest, **kwargs):
    if not any(p.item.room_definitions.count() != 0 for p in list(get_cart(request))):
        return

    cs = cart_session(request)

    template = get_template("pretix_roomsharing/checkout_confirm.html")
    ctx = {
        "mode": cs.get("room_mode"),
        "request": request,
    }
    if cs.get("room_mode") in ["create", "join"]:
        try:
            ctx["room"] = OrderRoom.objects.get(pk=cs.get("room_join")).room
        except OrderRoom.DoesNotExist:
            return
    return template.render(ctx)


@receiver(order_info, dispatch_uid="room_order_info")
def order_info(sender: Event, order: Order, **kwargs):
    if not any(p.item.room_definitions.count() != 0 for p in order.positions.all()):
        return

    template = get_template("pretix_roomsharing/order_info.html")

    ctx = {
        "order": order,
        "event": sender,
    }

    # Show link for user to change room
    order_has_room = False
    if order.meta_info_data["room_mode"] in ["create", "join", "none"]:
        order_has_room = True
    ctx["order_has_room"] = order_has_room

    if not order_has_room:
        return

    # Show current room details
    try:
        order_room = order.orderroom
        room = order_room.room
        fellows_orders = room.orderrooms.filter(
            order__status__in=(Order.STATUS_PENDING, Order.STATUS_PAID),
        ).exclude(pk=order_room.pk)

        display = sender.settings.get("roomsharing_room_mate_display")
        variable = get_variables(sender)[display]
        evaluate = variable["evaluate"]
        room_mates = [evaluate(o.order.all_positions.first(), o.order, sender) for o in fellows_orders]

        ctx["room"] = room
        ctx["is_admin"] = order_room.is_admin
        ctx["fellows"] = room_mates
    except OrderRoom.DoesNotExist:
        pass

    return template.render(ctx)


@receiver(control_order_info, dispatch_uid="room_control_order_info")
def control_order_info(sender: Event, request, order: Order, **kwargs):
    template = get_template("pretix_roomsharing/control_order_info.html")

    ctx = {
        "order": order,
        "event": sender,
        "request": request,
    }
    try:
        c = order.orderroom
        ctx["room"] = c.room
        ctx["is_admin"] = c.is_admin
    except OrderRoom.DoesNotExist:
        pass

    return template.render(ctx, request=request)


@receiver(signal=logentry_display, dispatch_uid="room_logentry_display")
def shipping_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_roomsharing"):
        return

    plains = {
        "pretix_roomsharing.order.left": _("The user left a room."),
        "pretix_roomsharing.order.joined": _("The user joined a room."),
        "pretix_roomsharing.order.created": _("The user created a new room."),
        "pretix_roomsharing.order.changed": _("The user changed a room password."),
        "pretix_roomsharing.order.deleted": _("The room has been deleted."),
        "pretix_roomsharing.room.deleted": _("The room has been changed."),
        "pretix_roomsharing.room.changed": _("The room has been deleted."),
    }

    if logentry.action_type in plains:
        return plains[logentry.action_type]


@receiver(nav_event, dispatch_uid="room_nav")
def control_nav_event(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_orders", request=request
    ):
        return []
    return [
        {
            "label": _("Roomsharing"),
            "url": reverse(
                "plugins:pretix_roomsharing:event.room.list",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.event.organizer.slug,
                },
            ),
            "active": False,
            "icon": "group",
            'children': [
                {
                    'label': _('Rooms'),
                    'url': reverse('plugins:pretix_roomsharing:event.room.list', kwargs={
                        'event': request.event.slug,
                        'organizer': request.event.organizer.slug,
                    }),
                    'active': (
                            url.namespace == 'plugins:pretix_roomsharing'
                            and 'event.room.' in url.url_name
                    ),
                },
                {
                    'label': _('Room Definitions'),
                    'url': reverse('plugins:pretix_roomsharing:event.room_definition.list', kwargs={
                        'event': request.event.slug,
                        'organizer': request.event.organizer.slug,
                    }),
                    'active': (
                            url.namespace == 'plugins:pretix_roomsharing'
                            and 'event.room_definition.' in url.url_name
                    ),
                },
            ]
        }
    ]


@receiver(post_delete, sender=OrderRoom)
def post_order_room_delete(sender, instance, *args, **kwargs):
    with transaction.atomic():
        if instance.room:
            instance.room.touch()


@receiver(validate_order, dispatch_uid="room_validate_order")
def room_validate_order(sender: Event, payments, positions: QuerySet[CartPosition], email, locale, invoice_address, meta_info, customer, **kwargs):
    room_create = meta_info.get("room_create", None)
    room = None
    try:
        room = Room.objects.get(pk=room_create)
    except Room.DoesNotExist:
        pass

    if room and not room.is_valid():
        raise OrderError(_("Room Validation Error. Invalid room. Try to do the room step again."))

    room_join = meta_info.get("room_join", None)
    order_room = None
    try:
        order_room = OrderRoom.objects.get(pk=room_join)
    except OrderRoom.DoesNotExist:
        pass

    if order_room:
        if not order_room.is_valid():
            raise OrderError(_("Room Validation Error. Invalid Order Room. Try to do the room step again."))
        elif not positions.filter(item__in=order_room.room.room_definition.items.all()).exists():
            raise OrderError(_("Room Validation Error. No items in cart which grant a room. Try to do the room step again."))

    room_mode = meta_info.get("room_mode", None)
    match room_mode:
        case "join":
            if room_create or not room_join:
                raise OrderError(_("Room Validation Error. Room mode is join but no joined room or room created. Try to do the room step again."))
        case "create":
            if not room_create or not room_join:
                raise OrderError(_("Room Validation Error. Room mode is create but no room created or room joined. Try to do the room step again."))
        case "none" | None:
            if room_create or room_join:
                raise OrderError(_("Room Validation Error. Room mode is none but no room created or no room joined. Try to do the room step again."))
        case _:
            raise OrderError(_("Room Validation Error. Invalid room mode. Try to do the room step again."))


class RoomSearchForm(FilterForm):
    room_name = forms.CharField(
        label=_("Room name"), required=False, help_text=_("Exact matches only")
    )

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        super().__init__(*args, **kwargs)
        del self.fields["ordering"]

    def filter_qs(self, qs):
        fdata = self.cleaned_data
        qs = super().filter_qs(qs)
        if fdata.get("room_name"):
            qs = qs.filter(orderroom__room__name__iexact=fdata.get("room_name"))
        return qs


@receiver(layout_text_variables, dispatch_uid="room_layout_text_variables")
def room_layout_text_variables(sender, *args, **kwargs):
    def get_room_name(order: Order):
        if hasattr(order, "orderroom"):
            return order.orderroom.room.name
        else:
            ""

    def get_room_type(order: Order):
        if hasattr(order, "orderroom"):
            return order.orderroom.room.room_definition.type
        else:
            ""

    return {
        "room_name": {
            "label": _("Room Name"),
            "editor_sample": "Dragon Cave",
            "evaluate": lambda orderposition, order, event: get_room_name(order),
        },
        "room_type": {
            "label": _("Room Type"),
            "editor_sample": "Double Bedroom",
            "evaluate": lambda orderposition, order, event: get_room_type(order),
        },
    }

@receiver(register_data_exporters, dispatch_uid="room_data_exporter")
def room_register_data_exporter(sender, **kwargs):
    return RoomExporter


try:
    from pretix.control.signals import order_search_forms

    @receiver(order_search_forms, dispatch_uid="room_order_search")
    def control_order_search(sender, request, **kwargs):
        return RoomSearchForm(
            data=request.GET,
            event=sender,
            prefix="rooms",
        )

except ImportError:
    pass

settings_hierarkey.add_default("roomsharing_room_mate_display", "order", str)
