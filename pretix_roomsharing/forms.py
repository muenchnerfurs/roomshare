from django import forms
from pretix.base.forms import I18nModelForm
from pretix.control.forms import ItemMultipleChoiceField

from pretix_roomsharing.models import RoomDefinition


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
        fields = ['name', 'items', 'capacity', 'max_rooms']
        widgets = {'items': forms.CheckboxSelectMultiple(attrs={'class': 'scrolling-multiple-choice'}), }
        field_classes = {'items': ItemMultipleChoiceField}
