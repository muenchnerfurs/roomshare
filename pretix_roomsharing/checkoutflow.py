from django import forms
from django.contrib import messages
from django.db.transaction import atomic
from django.shortcuts import redirect
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from pretix.base.models import SubEvent
from pretix.presale.checkoutflow import TemplateFlowStep
from pretix.presale.views import CartMixin, get_cart
from pretix.presale.views.cart import cart_session, get_or_create_cart_id

from .models import Room, RoomDefinition, OrderRoom


class RoomCreateForm(forms.Form):
    error_messages = {
        "duplicate_name": _(
            "There already is a room with that name. If you want to join a room already created "
            "by your friends, please choose to join a room instead of creating a new one."
        ),
        "required": _("This field is required."),
    }

    room_definition = forms.ChoiceField(label=_("Room Type"), required=False)
    name = forms.CharField(
        max_length=190,
        label=_("Room name"),
        required=False,
    )
    password = forms.CharField(
        max_length=190, label=_("Room password"), min_length=3, required=False
    )

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        self.room = kwargs.pop("current", None)
        self.room_definitions = kwargs.pop("room_definitions", None)
        super().__init__(*args, **kwargs)
        self.fields['room_definition'].choices = self.room_definitions
        if self.room_definitions:
            self.fields['room_definition'].initial = self.room_definitions[0][0]
            self.fields['room_definition'].disabled = len(self.room_definitions) == 1
        else:
            self.fields['room_definition'].initial = ("", "")
            self.fields['room_definition'].disabled = True

    def clean_name(self):
        name = self.cleaned_data.get("name")
        if not name:
            raise forms.ValidationError(
                self.error_messages["required"], code="required"
            )

        room = Room.objects.filter(event=self.event, name=name).exclude(pk=(self.room.pk if self.room else 0))
        if room.exists() and room.first().is_valid():
            raise forms.ValidationError(
                self.error_messages["duplicate_name"], code="duplicate_name"
            )
        return name

    def clean_password(self):
        password = self.cleaned_data.get("password")

        if not password:
            raise forms.ValidationError(
                self.error_messages["required"], code="required"
            )

        return password


class RoomJoinForm(forms.Form):
    error_messages = {
        "room_not_found": _(
            "This room does not exist. Are you sure you entered the name correctly?"
        ),
        "required": _("This field is required."),
        "pw_mismatch": _(
            "The password does not match. Please enter the password exactly as your friends send it."
        ),
    }

    name = forms.CharField(
        max_length=190,
        label=_("Room name"),
        required=False,
    )
    password = forms.CharField(
        max_length=190,
        label=_("Room password"),
        min_length=3,
        widget=forms.PasswordInput,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        super().__init__(*args, **kwargs)

    def clean(self):
        name = self.cleaned_data.get("name")
        password = self.cleaned_data.get("password")

        if not name:
            raise forms.ValidationError(
                {
                    "name": self.error_messages["required"],
                },
                code="required",
            )

        if not password:
            raise forms.ValidationError(
                {
                    "name": self.error_messages["required"],
                },
                code="required",
            )

        try:
            room = Room.objects.get(event=self.event, name=name)
        except Room.DoesNotExist:
            raise forms.ValidationError(
                {
                    "name": self.error_messages["room_not_found"],
                },
                code="room_not_found",
            )

        if not room.is_valid():
            raise forms.ValidationError(
                {
                    "name": self.error_messages["room_not_found"],
                },
                code="room_not_found",
            )

        if room.password != password:
            raise forms.ValidationError(
                {
                    "password": self.error_messages["pw_mismatch"],
                },
                code="pw_mismatch",
            )

        self.cleaned_data["room"] = room
        return self.cleaned_data


class RoomStep(CartMixin, TemplateFlowStep):
    priority = 180
    identifier = "room"
    template_name = "pretix_roomsharing/checkout_room.html"
    icon = "group"
    label = pgettext_lazy("checkoutflow", "Room")

    def post(self, request):
        self.request = request

        if not self.has_applicable_positions:
            return self.render()

        previous_room_mode = self.cart_session.get("room_mode", "none")
        room_mode = request.POST.get("room_mode", "none")
        if room_mode == "nochange":
            self.cart_session["room_mode"] = previous_room_mode
        else:
            self.cart_session["room_mode"] = room_mode

        if room_mode == "join" and self.join_form.is_valid():
            return self.post_room_join(request)
        elif room_mode == "create" and self.create_form.is_valid():
            return self.post_room_create(request)
        elif room_mode == "none":
            return self.post_room_none(request)
        elif room_mode == "nochange":
            return redirect(self.get_next_url(request))

        messages.error(
            self.request,
            _("We couldn't handle your input, please check below for errors."),
        )
        return self.render()

    @atomic
    def post_room_none(self, request):
        if pk := self.cart_session.get("room_join"):
            try:
                OrderRoom.objects.get(event=self.event, pk=pk).delete()
            except OrderRoom.DoesNotExist:
                pass

        return redirect(self.get_next_url(request))

    @atomic
    def post_room_join(self, request):
        order_room = None
        try:
            order_room = OrderRoom.objects.get(cart_id=get_or_create_cart_id(request))
        except OrderRoom.DoesNotExist:
            pass

        cleaned_data = self.join_form.cleaned_data
        room = cleaned_data["room"]

        if order_room and order_room.room.pk == room.pk:
            return redirect(self.get_next_url(request))

        if not room.has_capacity():
            messages.error(
                self.request,
                _("The chosen room is already full. Please choose another one."),
            )
            return self.render()

        if order_room:
            order_room.room = room
            order_room.save()
            room.touch()
        else:
            order_room = OrderRoom.objects.create(cart_id=get_or_create_cart_id(request), room=room, is_admin=False)

        self.cart_session["room_join"] = order_room.pk
        return redirect(self.get_next_url(request))

    @atomic
    def post_room_create(self, request):
        room_definition = RoomDefinition.objects.get(event=self.event, pk=int(self.create_form.cleaned_data["room_definition"]))
        if not room_definition.is_available():
            messages.error(
                self.request,
                _("No more rooms of this type available. Please choose another one."),
            )
            return self.render()

        if pk := self.cart_session.get("room_join"):
            try:
                OrderRoom.objects.get(pk=pk).delete()
            except OrderRoom.DoesNotExist:
                pass

        cleaned_data = self.create_form.cleaned_data
        try:
            room = Room.objects.get(event=self.event, name=cleaned_data["name"])
            room.password = cleaned_data["password"]
            room.room_definition = room_definition
            room.save()
        except Room.DoesNotExist:
            room = Room.objects.create(event=self.event, room_definition=room_definition, name=cleaned_data["name"], password=cleaned_data["password"])
            pass

        order_room = OrderRoom.objects.create(order=None, cart_id=get_or_create_cart_id(request), room=room, is_admin=True)

        self.cart_session["room_create"] = room.pk
        self.cart_session["room_join"] = order_room.pk
        return redirect(self.get_next_url(request))

    @cached_property
    def create_form(self):
        initial = {}
        current = None
        if (
            self.cart_session.get("room_mode") == "create"
            and "room_create" in self.cart_session
        ):
            try:
                current = Room.objects.get(
                    event=self.event, pk=self.cart_session["room_create"]
                )
            except Room.DoesNotExist:
                pass
            else:
                initial["name"] = current.name
                initial["password"] = current.password

        return RoomCreateForm(
            event=self.event,
            prefix="create",
            initial=initial,
            current=current,
            room_definitions=self.available_room_definitions,
            data=self.request.POST
            if self.request.method == "POST"
            and self.request.POST.get("room_mode") == "create"
            else None,
        )

    @cached_property
    def join_form(self):
        initial = {}
        if (
            self.cart_session.get("room_mode") == "join"
            and "room_join" in self.cart_session
        ):
            try:
                room = Room.objects.get(
                    event=self.event, pk=self.cart_session["room_join"]
                )
            except Room.DoesNotExist:
                pass
            else:
                initial["name"] = room.name
                initial["password"] = room.password

        return RoomJoinForm(
            event=self.event,
            prefix="join",
            initial=initial,
            data=self.request.POST
            if self.request.method == "POST"
            and self.request.POST.get("room_mode") == "join"
            else None,
        )

    @cached_property
    def cart_session(self):
        return cart_session(self.request)

    @cached_property
    def available_room_definitions(self):
        items = [position.item for position in self.positions]
        room_definitions = (definition for item in items for definition in item.room_definitions.all())
        return [(definition.id, definition.name) for definition in room_definitions if definition.is_available()]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["create_form"] = self.create_form
        ctx["join_form"] = self.join_form
        ctx["cart"] = self.get_cart()

        selected = ctx["selected"] = self.cart_session.get("room_mode", "none")
        if selected in ["create", "join"]:
            ctx["show_nochange"] = True
            ctx["selected"] = "nochange"
        else:
            ctx["selected"] = selected

        ctx["order_has_room"] = True
        ctx["create_disabled"] = len(self.available_room_definitions) == 0
        return ctx

    def is_completed(self, request, warn=False):
        if (
            request.event.has_subevents
            and cart_session(request).get("room_mode") == "join"
            and "room_join" in cart_session(request)
        ):
            try:
                room = Room.objects.get(
                    event=self.event, pk=cart_session(request)["room_join"]
                )
                room_subevents = set(
                    c["order__all_positions__subevent"]
                    for c in room.orderrooms.filter(
                        order__all_positions__canceled=False
                    )
                    .values("order__all_positions__subevent")
                    .distinct()
                )
                # TODO: Validation of same room type
                # TODO: Validation of max room quantity?
                if room_subevents:
                    cart_subevents = set(
                        c["subevent"]
                        for c in get_cart(request).values("subevent").distinct()
                    )
                    if any(c not in room_subevents for c in cart_subevents):
                        if warn:
                            messages.warning(
                                request,
                                _(
                                    """
                                    You requested to join a room that participates in "{subevent_room}",
                                    while you chose to participate in "{subevent_cart}".
                                    Please choose a different room.
                                """
                                ).format(
                                    subevent_room=SubEvent.objects.get(
                                        pk=list(room_subevents)[0]
                                    ).name,
                                    subevent_cart=SubEvent.objects.get(
                                        pk=list(cart_subevents)[0]
                                    ).name,
                                ),
                            )
                        return False
            except Room.DoesNotExist:
                pass

        return "room_mode" in cart_session(request)

    def is_applicable(self, request):
        self.request = request
        return self.has_applicable_positions

    @cached_property
    def has_applicable_positions(self):
        return any(p.item.room_definitions.count() != 0 for p in self.positions)
