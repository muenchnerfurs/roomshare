from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import I18nModelForm, SettingsForm
from pretix.base.pdf import get_variables
from pretix.control.forms import ItemMultipleChoiceField
from pretix_roomsharing.models import RoomDefinition, OrderRoom


class RoomDefinitionForm(I18nModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['items'].queryset = self.instance.event.items.all()
        self.fields['items'].required = True

    def clean(self):
        d = super().clean()
        return d

    class Meta:
        model = RoomDefinition
        fields = ['name', 'items', 'capacity', 'extra_capacity', 'max_rooms']
        widgets = {'items': forms.CheckboxSelectMultiple(attrs={'class': 'scrolling-multiple-choice'}), }
        field_classes = {'items': ItemMultipleChoiceField}


class OrderRoomForm(I18nModelForm):
    code = forms.CharField(label='Order Code', max_length=100)

    class Meta:
        fields = []
        model = OrderRoom


class RoomsharingSettingsForm(SettingsForm):
    roomsharing_room_mate_display = forms.CharField(
        label=_("Room mate display"),
        help_text=_("How room mates are displayed to users. See pdf output for possible values."),
        required=True
    )
    roomsharing_room_host_random_control = forms.BooleanField(
        label=_("Allow room hosts to control random assignment to optional beds"),
        help_text=_("When enabled, each room host can individually control whether extra beds are filled by random assignment at the end of the booking period."),
        required=False
    )

    def clean_roomsharing_room_mate_display(self):
        value = self.cleaned_data.get("roomsharing_room_mate_display")
        variables = get_variables(self.obj)
        if not variables.get(value, None):
            raise forms.ValidationError(_("Invalid value"), code="invalid")

        return value


class RandomizeRoomsConfirmationForm(forms.Form):
    force_assignment = forms.BooleanField(
        label=_("Force assignment."),
        required=False,
        help_text=_("Normally this assignment fails, if there is not enough total available capacity to accommodate all orders, and no orders will be assigned. With this enabled, all orders which can be assigned, will be assigned.")
    )
