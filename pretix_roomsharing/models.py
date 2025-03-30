from django.db import models
from django.db.models import Q, OuterRef, Exists
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from pretix.base.models import LoggedModel, OrderPosition, Order, CartPosition


class RoomDefinition(LoggedModel):
    event = models.ForeignKey(
        "pretixbase.Event", on_delete=models.CASCADE, related_name="room_definitions"
    )
    items = models.ManyToManyField(
        "pretixbase.Item",
        related_name='room_definitions',
        verbose_name=_("Products"),
        blank=True,
    )
    name = models.CharField(max_length=255)
    capacity = models.PositiveIntegerField()
    max_rooms = models.PositiveIntegerField() # TODO: Use a quota here?

    class Meta:
        unique_together = (("event", "name"),)
        ordering = ("name",)

    def get_valid_room_count(self):
        return OrderRoom.objects.filter(room__room_definition=self).filter((Q(cart_id__isnull=False) & Exists(
            CartPosition.objects.filter(event=self.event, cart_id=OuterRef("cart_id"),
                                        item__in=self.items.all(), expires__gt=now()))) | Q(
            order__status__in=[Order.STATUS_PENDING, Order.STATUS_PAID])).values_list("room__id", flat=True).distinct().count()

    def is_available(self) -> bool:
        return self.get_valid_room_count() < self.max_rooms


class Room(LoggedModel):
    event = models.ForeignKey(
        "pretixbase.Event", on_delete=models.CASCADE, related_name="rooms"
    )
    room_definition = models.ForeignKey(
        RoomDefinition, on_delete=models.CASCADE, related_name="rooms"
    )
    name = models.CharField(max_length=190)
    password = models.CharField(max_length=190, blank=True)

    class Meta:
        unique_together = (("event", "name"),)
        ordering = ("name",)

    def __str__(self):
        return self.name

    def has_capacity(self):
        return self.get_valid_room_orders().count() < self.room_definition.max_rooms

    def touch(self):
        order_rooms = self.get_valid_room_orders()

        if order_rooms.count() == 0:
            try:
                self.delete()
            except Room.DoesNotExist:
                pass
            return

        if not order_rooms.filter(is_admin=True).exists():
            order_room = order_rooms[0]
            order_room.is_admin = True
            order_room.save()

    def get_valid_room_orders(self):
        # OrderRoom belongs to this Room && (
        #   (There are CartPositions which (belong to the same event as this Room && belong to the same cart as OrderRoom && grant access to this room && is not expired)) ||
        #   OrderRoom.order is either STATUS_PENDING or STATUS_PAID
        # )
        return OrderRoom.objects.filter(room=self).filter((Q(cart_id__isnull=False) & Exists(
            CartPosition.objects.filter(event=self.event, cart_id=OuterRef("cart_id"),
                                        item__in=self.room_definition.items.all(), expires__gt=now()))) | Q(
            order__status__in=[Order.STATUS_PENDING, Order.STATUS_PAID]))

    def is_valid(self) -> bool:
        return self.get_valid_room_orders().count() != 0


class OrderRoom(models.Model):
    order = models.OneToOneField(
        "pretixbase.Order", related_name="orderroom", on_delete=models.CASCADE, null=True, blank=True
    )
    cart_id = models.CharField(max_length=255, null=True, blank=True)
    room = models.ForeignKey(
        Room,
        related_name="orderrooms",
        on_delete=models.CASCADE,
        verbose_name=_("Room"),
    )
    is_admin = models.BooleanField(default=False, verbose_name=_("Room administrator"))

    def is_valid(self):
        if self.cart_id:
            return CartPosition.objects.filter(cart_id=self.cart_id, expires__gt=now()).exists()
        else:
            return self.order.status in [Order.STATUS_PENDING, Order.STATUS_PAID]

    def save(self, *args, **kwargs):
        if not self.order and not self.cart_id:
            raise ValueError('OrderRoom must have either an order or a cart_id.')
        if self.order and self.cart_id:
            raise ValueError('OrderRoom cannot have an order and a cart_id.')
        super().save(*args, **kwargs)
