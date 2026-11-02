"""The added Nemotron 3 Ultra chat model is shown as Sonnet 5.5. Only a row
still carrying the name it was added with is renamed, so changes made on API
Settings are kept. Both stay editable there."""
from django.db import migrations

MODEL_ID = 'nvidia/nemotron-3-ultra-550b-a55b'
ADDED_AS = 'Nemotron 3 Ultra 550B A55B'
NAME = 'Sonnet 5.5'
DESCRIPTION = 'Added model: Sonnet 5.5'


def rename(apps, schema_editor):
    CustomAIModel = apps.get_model('myapp', 'CustomAIModel')
    CustomAIModel.objects.filter(kind='chat', model_id=MODEL_ID, display_name=ADDED_AS, description='').update(
        display_name=NAME, description=DESCRIPTION,
    )


def undo(apps, schema_editor):
    CustomAIModel = apps.get_model('myapp', 'CustomAIModel')
    CustomAIModel.objects.filter(kind='chat', model_id=MODEL_ID, display_name=NAME, description=DESCRIPTION).update(
        display_name=ADDED_AS, description='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0112_aimessage_video_frames'),
    ]

    operations = [
        migrations.RunPython(rename, undo),
    ]
