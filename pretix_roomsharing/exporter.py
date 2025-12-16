from django.utils.translation import (gettext as _, gettext_lazy)
from pretix.base.exporter import ListExporter


class RoomExporter(ListExporter):
    verbose_name = 'Room list'
    identifier = 'roomlist'
    description = gettext_lazy('Download a spreadsheet of all rooms and their details.')
    category = gettext_lazy('Roomsharing')

    def iterate_list(self, form_data):
        headers = [
            _('Order Id'), _('Order Code'), _('Order Status'), _('Customer Name'), _('Order Email'), _('Internal Room Name'), _('Room Id'), _('Is Admin')
        ]
        yield headers

        for room in self.event.rooms.order_by('room_definition__name'):
            for occupant in room.orderrooms.order_by('id', '-is_admin'):
                order = occupant.order
                name = order.invoice_address.name_cached
                yield [order.id, order.code, order.status, name, order.email, room.room_definition.name, room.id, occupant.is_admin]
