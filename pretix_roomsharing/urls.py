from django.urls import path, re_path

from .views import (
    ControlRoomChange,
    MetricsView,
    OrderRoomChange,
    RoomDelete,
    RoomDetail,
    RoomList,
    SettingsView,
    StatsView, RoomDefinitionList, RoomDefinitionUpdate, RoomDefinitionDelete, RoomDefinitionCreate, RoomRemoveOrder,
    RoomAddOrder, RandomizeView,
)

urlpatterns = [
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/roomsharing$",
        SettingsView.as_view(),
        name="control.room.settings",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/roomsharing/randomize$",
        RandomizeView.as_view(),
        name="control.room.randomize",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/orders/(?P<code>[0-9A-Z]+)/room$",
        ControlRoomChange.as_view(),
        name="control.order.room.modify",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/stats/",
        StatsView.as_view(),
        name="event.stats",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/",
        RoomList.as_view(),
        name="event.room.list",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/<int:pk>/",
        RoomDetail.as_view(),
        name="event.room.detail",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/<int:pk>/delete",
        RoomDelete.as_view(),
        name="event.room.delete",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/<int:pk>/remove/<str:order_code>/",
        RoomRemoveOrder.as_view(),
        name="event.room.remove_order",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/rooms/<int:pk>/add",
        RoomAddOrder.as_view(),
        name="event.room.add_order",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/roomdefinitions/",
        RoomDefinitionList.as_view(),
        name="event.room_definition.list",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/roomdefinitions/<int:pk>/",
        RoomDefinitionUpdate.as_view(),
        name="event.room_definition.edit",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/roomdefinitions/add",
        RoomDefinitionCreate.as_view(),
        name="event.room_definition.add",
    ),
    path(
        r"control/event/<str:organizer>/<str:event>/roomdefinitions/<int:pk>/delete",
        RoomDefinitionDelete.as_view(),
        name="event.room_definition.delete",
    ),
    path(
        r"metrics/rooms/<str:organizer>/<str:event>/",
        MetricsView.as_view(),
        name="metrics",
    ),
]

event_patterns = [
    re_path(
        r"^order/(?P<order>[^/]+)/(?P<secret>[A-Za-z0-9]+)/room/modify$",
        OrderRoomChange.as_view(),
        name="event.order.room.modify",
    ),
]
