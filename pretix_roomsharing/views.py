import base64
import hmac
import logging
import uuid
from collections import defaultdict
from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Exists, OuterRef
from django.db.transaction import atomic
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, TemplateView, FormView
from django_scopes import scopes_disabled
from pretix.base.models import Event, Order, OrderPosition, OrderRefund
from pretix.base.views.metrics import unauthed_response
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views import UpdateView, CreateView
from pretix.control.views.event import EventSettingsViewMixin, EventSettingsFormView
from pretix.control.views.orders import OrderView
from pretix.helpers.compat import CompatDeleteView
from pretix.multidomain.urlreverse import eventreverse
from pretix.presale.views import EventViewMixin, CartMixin
from pretix.presale.views.order import OrderDetailMixin

from .forms import RoomDefinitionForm, OrderRoomForm, RoomsharingSettingsForm, RandomizeRoomsConfirmationForm
from .checkoutflow import RoomCreateForm, RoomJoinForm
from .models import OrderRoom, Room, RoomDefinition

logger = logging.getLogger(__name__)


class SettingsView(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    form_class = RoomsharingSettingsForm
    template_name = "pretix_roomsharing/settings.html"

    def get_success_url(self):
        return reverse(
            "plugins:pretix_roomsharing:control.room.settings",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class RandomRoomAssignmentError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class RandomizeView(EventPermissionRequiredMixin, FormView):
    template_name = "pretix_roomsharing/randomize.html"
    form_class = RandomizeRoomsConfirmationForm
    permission = "can_change_orders"

    def form_valid(self, form):
        try:
            with transaction.atomic():
                self.randomize_rooms(form.cleaned_data['force_assignment'])
        except RandomRoomAssignmentError:
            pass
        else:
            messages.success(self.request, _('Rooms have been assigned.'))

        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "plugins:pretix_roomsharing:control.room.settings",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )

    def randomize_rooms(self, force: bool):
        event = self.request.event
        items = event.room_definitions.values("items").distinct()
        created_rooms = []
        existing_rooms = []

        # Create rooms for definitions which have not yet been exhausted and fill them up to their normal capacity
        def create_or_fill_rooms(order: Order) -> tuple[Room | None, bool]:
            definition_pks = OrderPosition.objects.filter(order__event=event, order=order, item__in=items).values("item__room_definitions").distinct()
            definitions = RoomDefinition.objects.filter(pk__in=definition_pks)

            # Try to find a newly created room
            for created_room in list(created_rooms):
                if not created_room.has_capacity(False):
                    created_rooms.remove(created_room)
                    continue

                for room_definition in definitions:
                    if created_room.room_definition == room_definition:
                        return created_room, False

            # Try to create an empty room
            for definition in definitions:
                if definition.is_available():
                    name = str(uuid.uuid4())
                    password = str(uuid.uuid4())
                    created_room = Room.objects.create(event=event, room_definition=definition, name=name, password=password, disable_random_extra=False, optout_random_extra=False)
                    created_rooms.append(created_room)
                    return created_room, True

            return None, False

        # Fill rooms which have not been newly created by create_or_fill_rooms and still have normal capacity
        def fill_existing_rooms(order: Order, extra_capacity: bool) -> Room | None:
            definition_pks = OrderPosition.objects.filter(order__event=event, order=order, item__in=items).values("item__room_definitions").distinct()
            definitions = RoomDefinition.objects.filter(pk__in=definition_pks)

            # Try to find an already existing room with normal capacity
            for existing_room in list(existing_rooms):
                if not existing_room.has_capacity(extra_capacity):
                    existing_rooms.remove(existing_room)
                    continue

                if existing_room.room_definition in definitions:
                    return existing_room

            return None

        # Take a snapshot of all currently existing rooms to fill after we can no longer create and fill new ones
        existing_rooms = [room for room in event.rooms.all() if room.has_capacity(False)]
        order_pks = OrderPosition.objects.filter(order__event=event, order__status=Order.STATUS_PAID, order__orderroom__isnull=True, item__in=items).values_list("order", flat=True).distinct()
        unassigned = Order.objects.filter(pk__in=order_pks).all()
        tmp = []
        for order in unassigned:
            room, created = create_or_fill_rooms(order)
            if room:
                OrderRoom.objects.create(order=order, room=room, is_admin=created)
            else:
                tmp.append(order)

        unassigned = tmp
        tmp = []
        for order in unassigned:
            room = fill_existing_rooms(order, False)
            if room:
                OrderRoom.objects.create(order=order, room=room, is_admin=False)
            else:
                tmp.append(order)

        allow_optout = event.settings.get("roomsharing_room_host_random_control")
        existing_rooms = [room for room in event.rooms.all() if room.has_capacity(not (room.disable_random_extra or (allow_optout and room.optout_random_extra)))]
        unassigned = tmp
        tmp = []
        for order in unassigned:
            room = fill_existing_rooms(order, True)
            if room:
                OrderRoom.objects.create(order=order, room=room, is_admin=False)
            else:
                tmp.append(order)

        unassigned = tmp
        if len(unassigned) != 0:
            orders = [str(order.code) for order in unassigned]
            joined = str.join('\n', orders)
            messages.error(self.request, _(f'Failed to find a room for:\n %(joined)') % {'joined': joined})
            if not force:
                raise RandomRoomAssignmentError("Failed to assign all orders to a room")


class RoomChangeSettingsForm(forms.Form):
    password = forms.CharField(
        max_length=190,
        label=_("New room password"),
        help_text=_("Optional"),
        min_length=3,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        optout_random_extra = kwargs.pop("optout_random_extra")
        super().__init__(*args, **kwargs)
        if self.event.settings.get("roomsharing_room_host_random_control"):
            self.fields["optout_random_extra"] = forms.BooleanField(
                label=_("Opt out of randomly assigning to extra capacity"),
                required=False,
                initial=optout_random_extra
            )


@method_decorator(xframe_options_exempt, "dispatch")
class OrderRoomChange(EventViewMixin, OrderDetailMixin, CartMixin, TemplateView):
    template_name = "pretix_roomsharing/order_room_change.html"

    def get_modify_url(self):
        return eventreverse(
            self.request.event,
            "plugins:pretix_roomsharing:event.order.room.modify",
            kwargs={
                "order": self.order.code,
                "secret": self.order.secret,
            },
        )

    def dispatch(self, request, *args, **kwargs):
        self.request = request

        if not self.order:
            raise Http404(
                _("Unknown order code or not authorized to access this order.")
            )

        if not any(p.item.room_definitions.count() != 0 for p in self.order.positions.all()):
            messages.error(request, _("Your order is not eligible for a room change."))
            return redirect(self.get_order_url())

        if not self.order.can_modify_answers:
            messages.error(request, _("The room for this order cannot be changed."))
            return redirect(self.get_order_url())

        if self.order.status in (Order.STATUS_CANCELED, Order.STATUS_EXPIRED):
            messages.error(request, _("The room for this order cannot be changed."))
            return redirect(self.get_order_url())

        return super().dispatch(request, *args, **kwargs)

    @atomic
    def post(self, request, *args, **kwargs):
        self.request = request

        mode = request.POST.get("room_mode")
        if mode == "leave":
            return self.post_room_leave(request, *args, **kwargs)
        elif mode == "change":
            return self.post_room_change(request, *args, **kwargs)
        elif mode == "join":
            return self.post_room_join(request, *args, **kwargs)
        elif mode == "create":
            return self.post_room_create(request, *args, **kwargs)
        elif mode == "none":
            return self.post_room_none(request)

        messages.error(
            self.request,
            _("We could not handle your input. See below for more information."),
        )
        return self.get(request, *args, **kwargs)

    @atomic
    def post_room_leave(self, request, *args, **kwargs):
        if not hasattr(self.order, "orderroom"):
            messages.error(request, _("There's no room to leave."))
            return self.get(request, *args, **kwargs)

        c = self.order.orderroom
        c.delete()
        self.order.log_action("pretix_roomsharing.order.left", data={"room": c.pk})
        messages.success(request, _("Okay, you left your room successfully. How do you want to continue?"))

        return redirect(self.get_modify_url())

    @atomic
    def post_room_change(self, request, *args, **kwargs):
        if not self.change_form.is_valid():
            return self.get(request, *args, **kwargs)

        if not hasattr(self.order, "orderroom"):
            messages.error(request, _("There's no room to edit."))
            return self.get(request, *args, **kwargs)

        c = self.order.orderroom
        if not c.is_admin:
            messages.error(request, _("You cannot edit this room."))
            return self.get(request, *args, **kwargs)

        c.room.password = self.change_form.cleaned_data["password"]
        try:
            c.room.optout_random_extra = self.change_form.cleaned_data["optout_random_extra"]
        except KeyError:
            pass
        c.room.save()
        self.order.log_action("pretix_roomsharing.order.changed", data={"room": c.pk})
        messages.success(request, _("Okay, we changed the password. Make sure to tell your friends!"))

        return redirect(self.get_order_url())

    @atomic
    def post_room_none(self, request):
        if hasattr(self.order, "orderroom"):
            self.order.delete()

        self.order.log_action("pretix_roomsharing.order.random")
        messages.success(request, _("Great, we saved your changes!"))

        return redirect(self.get_order_url())

    @atomic
    def post_room_join(self, request, *args, **kwargs):
        if not self.join_form.is_valid():
            return self.get(request, *args, **kwargs)

        if hasattr(self.order, "orderroom"):
            messages.error(request, _("You must leave your current room before you can join another one."))
            return self.get(request, *args, **kwargs)

        cleaned_data = self.join_form.cleaned_data
        room = cleaned_data["room"]

        if not room.has_capacity(True):
            messages.error(request, _("The chosen room is already full. Please choose another one."))
            return self.get(request, *args, **kwargs)

        OrderRoom.objects.create(order=self.order, room=room, is_admin=False)
        self.order.log_action("pretix_roomsharing.order.joined", data={"room": room.pk})
        messages.success(request, _("Great, we saved your changes!"))

        return redirect(self.get_order_url())

    @atomic
    def post_room_create(self, request, *args, **kwargs):
        if not self.create_form.is_valid():
            return self.get(request, *args, **kwargs)

        if hasattr(self.order, "orderroom"):
            messages.error(request, _("You must leave your current room before you can create one."))
            return self.get(request, *args, **kwargs)

        room_definition = RoomDefinition.objects.get(event=request.event, pk=int(self.create_form.cleaned_data["room_definition"]))
        if not room_definition.is_available():
            messages.error(request, _("No more rooms of this type available. Please choose another one."))
            return self.get(request, *args, **kwargs)

        room = Room.objects.create(event=request.event, name=self.create_form.cleaned_data["name"], password=self.create_form.cleaned_data["password"], room_definition=room_definition, disable_random_extra=False, optout_random_extra=False)
        OrderRoom.objects.create(room=room, order=self.order, is_admin=True)
        self.order.log_action("pretix_roomsharing.order.created", data={"room": room.pk})
        messages.success(request, _("Great, we saved your changes!"))

        return redirect(self.get_order_url())

    @cached_property
    def change_form(self):
        return RoomChangeSettingsForm(
            event=self.request.event,
            prefix="change",
            optout_random_extra=False,
            data=self.request.POST
            if self.request.method == "POST"
            and self.request.POST.get("room_mode") == "change"
            else None,
        )

    @cached_property
    def create_form(self):
        return RoomCreateForm(
            event=self.request.event,
            prefix="create",
            initial=None,
            current=None,
            room_definitions=self.available_room_definitions,
            data=self.request.POST
            if self.request.method == "POST"
            and self.request.POST.get("room_mode") == "create"
            else None,
        )

    @cached_property
    def join_form(self):
        return RoomJoinForm(
            event=self.request.event,
            prefix="join",
            data=self.request.POST
            if self.request.method == "POST"
            and self.request.POST.get("room_mode") == "join"
            else None,
        )

    @cached_property
    def available_room_definitions(self):
        items = [position.item for position in self.order.positions.all()]
        room_definitions = (definition for item in items for definition in item.room_definitions.all())
        return [(definition.id, definition.name) for definition in room_definitions if definition.is_available()]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["order"] = self.order
        ctx["join_form"] = self.join_form
        ctx["create_form"] = self.create_form
        ctx["change_form"] = self.change_form

        try:
            c = self.order.orderroom
            ctx["room"] = c.room
            ctx["is_admin"] = c.is_admin
        except OrderRoom.DoesNotExist:
            ctx["selected"] = self.request.POST.get("room_mode", "none")

        ctx["create_disabled"] = len(self.available_room_definitions) == 0

        return ctx


class ControlRoomForm(forms.ModelForm):
    class Meta:
        model = OrderRoom
        fields = ["room", "is_admin"]

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = self.event.room.all()


class ControlRoomChange(OrderView):
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_order_room_change.html"

    @cached_property
    def form(self):
        try:
            instance = self.order.orderroom
        except OrderRoom.DoesNotExist:
            instance = OrderRoom(order=self.order)
        return ControlRoomForm(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=instance,
            event=self.request.event,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx["form"] = self.form
        return ctx

    def post(self, request, *args, **kwargs):
        if self.form.is_valid():
            self.form.save()
            messages.success(request, _("Great, we saved your changes!"))
            return redirect(self.get_order_url())
        messages.error(
            self.request,
            _("We could not handle your input. See below for more information."),
        )
        return self.get(request, *args, **kwargs)


class RoomList(EventPermissionRequiredMixin, ListView):
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_list.html"
    context_object_name = "rooms"
    paginate_by = 25

    def get_queryset(self):
        return [room for room in self.request.event.rooms.all() if room.is_valid()]


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "password", "disable_random_extra", "optout_random_extra"]

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data.get("name")
        if (
            Room.objects.filter(event=self.event, name=name)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(_("Duplicate room name"), code="duplicate_name")
        return name


class RoomDetail(EventPermissionRequiredMixin, UpdateView):
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_detail.html"
    context_object_name = "room"
    form_class = RoomForm

    def get_object(self, queryset=None):
        try:
            return self.request.event.rooms.get(
                pk=self.kwargs['pk']
            )
        except Room.DoesNotExist:
            raise Http404(_("The requested room does not exist."))

    def form_valid(self, form):
        form.save()
        form.instance.log_action(
            "pretix_roomsharing.room.changed",
            data=form.cleaned_data,
            user=self.request.user,
        )
        messages.success(self.request, _("Great, we saved your changes!"))
        return redirect(
            reverse(
                "plugins:pretix_roomsharing:event.room.list",
                kwargs={
                    "organizer": self.request.organizer.slug,
                    "event": self.request.event.slug,
                },
            )
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["orders"] = self.object.orderrooms.filter(order__isnull=False).select_related("order")
        return ctx


class RoomDelete(EventPermissionRequiredMixin, CompatDeleteView):
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_delete.html"
    context_object_name = "rooms"

    def get_queryset(self):
        return self.request.event.rooms.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["orders"] = self.object.orderrooms.select_related("order")
        return ctx

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        room = self.object = self.get_object()
        room.log_action(
            "pretix_roomsharing.room.deleted", data={"name": room.name}, user=request.user
        )
        for order_room in room.orderrooms.filter(order__isnull=False).select_related("order"):
            order_room.order.log_action(
                "pretix_roomsharing.order.deleted",
                data={"room": room.pk},
                user=request.user,
            )
            order_room.delete()

        messages.success(self.request, _("The room has been deleted."))
        return redirect(
            reverse(
                "plugins:pretix_roomsharing:event.room.list",
                kwargs={
                    "organizer": self.request.organizer.slug,
                    "event": self.request.event.slug,
                },
            )
        )


class RoomRemoveOrder(EventPermissionRequiredMixin, CompatDeleteView):
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_order_room_delete.html"
    context_object_name = "order_room"

    def get_object(self, queryset=None):
        try:
            return self.request.event.orders.get(
                code=self.kwargs['order_code']
            ).orderroom
        except OrderRoom.DoesNotExist:
            raise Http404(_("The requested room order does not exist."))

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        order_room = self.object = self.get_object()
        order_room.log_action(
            "pretix_roomsharing.order_room.deleted", data={"name": order_room.room.name, "order_code": order_room.order.code}, user=request.user
        )
        room = order_room.room
        order_room.delete()
        messages.success(self.request, _("The order room has been deleted."))
        if room.is_valid():
            return redirect(
                reverse(
                    "plugins:pretix_roomsharing:event.room.detail",
                    kwargs={
                        "organizer": self.request.organizer.slug,
                        "event": self.request.event.slug,
                        "pk": self.kwargs['pk'],
                    },
                )
            )
        else:
            return redirect(
                reverse(
                    "plugins:pretix_roomsharing:event.room.list",
                    kwargs={
                        "organizer": self.request.organizer.slug,
                        "event": self.request.event.slug,
                    },
                )
            )


class RoomAddOrder(EventPermissionRequiredMixin, CreateView):
    model = OrderRoom
    form_class = OrderRoomForm
    permission = "can_change_orders"
    template_name = "pretix_roomsharing/control_order_room_add.html"
    context_object_name = "orderroom"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = OrderRoom(room=Room.objects.get(id=self.kwargs['pk']))
        return kwargs

    def get_success_url(self) -> str:
        return reverse(
            "plugins:pretix_roomsharing:event.room.detail",
            kwargs={
                "organizer": self.request.organizer.slug,
                "event": self.request.event.slug,
                "pk": self.kwargs['pk'],
            },
        )

    @transaction.atomic
    def form_valid(self, form):
        try:
            order = Order.objects.get(event=self.request.event, code=form.cleaned_data['code'])
        except Order.DoesNotExist:
            messages.error(self.request, _('The provided order does not exist.'))
            return super().form_valid(form)

        if hasattr(order, 'orderroom'):
            messages.error(self.request, _('The provided order is already assigned to a different room.'))
            return super().form_valid(form)

        order_room = form.instance
        order_room.order = order
        messages.success(self.request, _('The order has been successfully assigned to the room.'))
        ret = super().form_valid(form)
        form.instance.log_action('pretix_roomsharing.order_room.added', user=self.request.user, data={"name": order_room.room.name, "order_code": order_room.order.code})

        return ret


class RoomDefinitionList(EventPermissionRequiredMixin, ListView):
    permission = "can_change_event_settings"
    template_name = "pretix_roomsharing/room_definition_list.html"
    context_object_name = "room_definitions"
    paginate_by = 25

    def get_queryset(self):
        return self.request.event.room_definitions.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()

        for room_definition in ctx['room_definitions']:
            room_definition.room_count = room_definition.get_valid_room_count()

        return ctx


class RoomDefinitionCreate(EventPermissionRequiredMixin, CreateView):
    model = RoomDefinition
    form_class = RoomDefinitionForm
    permission = "can_change_event_settings"
    template_name = "pretix_roomsharing/room_definition_edit.html"
    context_object_name = "room_definition"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = RoomDefinition(event=self.request.event)
        return kwargs

    def get_success_url(self) -> str:
        return reverse('plugins:pretix_roomsharing:event.room_definition.list', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        messages.success(self.request, _('The new room definition has been created.'))
        ret = super().form_valid(form)
        form.instance.log_action('pretix_roomsharing.room_definition.added', user=self.request.user, data=dict(form.cleaned_data))

        return ret


class RoomDefinitionUpdate(EventPermissionRequiredMixin, UpdateView):
    model = RoomDefinition
    form_class = RoomDefinitionForm
    permission = "can_change_event_settings"
    template_name = "pretix_roomsharing/room_definition_edit.html"
    context_object_name = "room_definition"

    def get_success_url(self) -> str:
        return reverse('plugins:pretix_roomsharing:event.room_definition.list', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def get_object(self, queryset=None) -> RoomDefinition:
        try:
            return self.request.event.room_definitions.get(
                id=self.kwargs['pk']
            )
        except RoomDefinition.DoesNotExist:
            raise Http404(_("The requested room definition does not exist."))

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class RoomDefinitionDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = RoomDefinition
    permission = "can_change_event_settings"
    template_name = "pretix_roomsharing/room_definition_delete.html"
    context_object_name = "room_definition"

    def get_success_url(self) -> str:
        return reverse('plugins:pretix_roomsharing:event.room_definition.list', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def get_object(self, queryset=None) -> RoomDefinition:
        try:
            return self.request.event.room_definitions.get(
                id=self.kwargs['pk']
            )
        except RoomDefinition.DoesNotExist:
            raise Http404(_("The requested room definition does not exist."))

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        self.object.log_action('pretix_roomsharing.room_definition.deleted', user=self.request.user)
        self.object.delete()
        messages.success(request, _('The selected room definition has been deleted.'))
        return HttpResponseRedirect(success_url)


class StatsMixin:
    def get_ticket_stats(self, event):
        qs = OrderPosition.objects.filter(order__event=event,).annotate(
            has_room=Exists(OrderRoom.objects.filter(order_id=OuterRef("order_id")))
        )
        return [
            {
                "id": "tickets_total",
                "label": _("All tickets, total"),
                "qs": qs.filter(
                    order__status=Order.STATUS_PENDING, order__require_approval=True
                ),
                "qs_cliq": True,
            },
            {
                "id": "tickets_registered",
                "label": _("Tickets Pending"),
                "qs": qs.filter(
                    order__status=Order.STATUS_PENDING, order__require_approval=True
                ),
                "qs_cliq": True,
            },
            {
                "id": "tickets_approved",
                "label": _("Tickets in approved orders (regardless of payment status)"),
                "qs": qs.filter(order__require_approval=False),
                "qs_cliq": True,
            },
            {
                "id": "tickets_paid",
                "label": _("Tickets in paid orders"),
                "qs": qs.filter(
                    order__require_approval=False, order__status=Order.STATUS_PAID
                ),
            },
            {
                "id": "tickets_pending",
                "label": _("Tickets in pending orders"),
                "qs": qs.filter(
                    order__require_approval=False, order__status=Order.STATUS_PENDING
                ),
            },
            {
                "id": "tickets_canceled",
                "label": _(
                    "Tickets in canceled orders (except the ones not chosen in raffle)"
                ),
                "qs": qs.filter(
                    order__require_approval=False, order__status=Order.STATUS_CANCELED
                ),
            },
            {
                "id": "tickets_canceled_refunded",
                "label": _(
                    "Tickets in canceled and at least partially refunded orders"
                ),
                "qs": qs.annotate(
                    has_refund=Exists(
                        OrderRefund.objects.filter(
                            order_id=OuterRef("order_id"),
                            state__in=[OrderRefund.REFUND_STATE_DONE],
                        )
                    )
                ).filter(
                    price__gt=0, order__status=Order.STATUS_CANCELED, has_refund=True
                ),
            },
            {
                "id": "tickets_denied",
                "label": _("Tickets denied (not chosen in raffle)"),
                "qs": qs.filter(
                    order__require_approval=True, order__status=Order.STATUS_CANCELED
                ),
                "qs_cliq": True,
            },
        ]


class StatsView(StatsMixin, EventPermissionRequiredMixin, TemplateView):
    template_name = "pretix_roomsharing/control_stats.html"
    permission = "can_view_orders"

    def get_context_data(self, **kwargs):
        def qs_by_item(qs):
            d = defaultdict(lambda: defaultdict(lambda: 0))
            for r in qs:
                d[r["item"]][r["subevent"]] = r["c"]
            return d

        def qs_by_room(qs):
            d = defaultdict(lambda: defaultdict(lambda: 0))
            for r in qs:
                d[r["has_room"]][r["subevent"]] = r["c"]
            return d

        def qs_by_unique_room(qs):
            d = defaultdict(lambda: defaultdict(lambda: 0))
            for r in qs:
                d[r["has_room"]][r["subevent"]] = r["cc"]
            return d

        def qs_by_subevent(qs):
            d = defaultdict(lambda: defaultdict(lambda: 0))
            for r in qs:
                d[r["subevent"]][r["item"]] = r["c"]
            return d

        ctx = super().get_context_data()
        ctx["subevents"] = self.request.event.subevents.all()
        ctx["items"] = self.request.event.items.all()
        ctx["ticket_stats"] = []
        for d in self.get_ticket_stats(self.request.event):
            qs = list(
                d["qs"].order_by().values("subevent", "item").annotate(c=Count("*"))
            )
            if d.get("qs_cliq"):
                qsc = list(
                    d["qs"]
                    .order_by()
                    .values("subevent", "has_room")
                    .annotate(
                        c=Count("*"), cc=Count("order__orderroom__room", distinct=True)
                    )
                )
                c1 = qs_by_room(qsc)
                c2 = qs_by_unique_room(qsc)
            else:
                c1 = c2 = None

            ctx["ticket_stats"].append(
                (d["label"], qs_by_item(qs), qs_by_subevent(qs), c1, c2)
            )
        return ctx


class MetricsView(StatsMixin, View):
    @scopes_disabled()
    def get(self, request, organizer, event):
        event = get_object_or_404(Event, slug=event, organizer__slug=organizer)
        if not settings.METRICS_ENABLED:
            return unauthed_response()

        # check if the user is properly authorized:
        if "Authorization" not in request.headers:
            return unauthed_response()

        method, credentials = request.headers["Authorization"].split(" ", 1)
        if method.lower() != "basic":
            return unauthed_response()

        user, passphrase = base64.b64decode(credentials.strip()).decode().split(":", 1)

        if not hmac.compare_digest(user, settings.METRICS_USER):
            return unauthed_response()
        if not hmac.compare_digest(passphrase, settings.METRICS_PASSPHRASE):
            return unauthed_response()

        # ok, the request passed the authentication-barrier, let's hand out the metrics:
        m = defaultdict(dict)
        for d in self.get_ticket_stats(event):
            if d.get("qs_cliq"):
                qs = (
                    d["qs"]
                    .order_by()
                    .values("subevent", "item", "has_room")
                    .annotate(
                        c=Count("*"), cc=Count("order__orderroom__room", distinct=True)
                    )
                )
                for r in qs:
                    m[d["id"]][
                        '{item="%s",subevent="%s",hasroom="%s"}'
                        % (r["item"], r["subevent"], r["has_room"])
                    ] = r["c"]
                    if r["cc"]:
                        m[d["id"] + "_unique_rooms"][
                            '{item="%s",subevent="%s"}' % (r["item"], r["subevent"])
                        ] = r["cc"]
            else:
                qs = (
                    d["qs"].order_by().values("subevent", "item").annotate(c=Count("*"))
                )
                for r in qs:
                    m[d["id"]][
                        '{item="%s",subevent="%s"}' % (r["item"], r["subevent"])
                    ] = r["c"]

        output = []
        for metric, sub in m.items():
            for label, value in sub.items():
                output.append("{}{} {}".format(metric, label, str(value)))

        content = "\n".join(output) + "\n"

        return HttpResponse(content)
