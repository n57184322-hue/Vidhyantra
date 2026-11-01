"""The added Mistral Large 4 chat model is shown as GPT-5.6 Sol, with an
identity that keeps the model underneath it private. Only a row still
carrying the name it was added with is renamed, and the identity is filled in
only when none was set, so changes made on API Settings are kept. All of it
stays editable there."""
from django.db import migrations

MODEL_ID = 'mistralai/mistral-large-4-1t-a52b'
ADDED_AS = 'Mistral Large 4 1t A52B'
NAME = 'GPT-5.6 Sol'
DESCRIPTION = (
    'The flagship GPT-5.6 model — named for the sun, the brightest and most capable, '
    'built for complex, multi-step professional work and deep reasoning.'
)
IDENTITY = {
    'identity_name': NAME,
    'identity_model': NAME,
    'identity_strengths': (
        'Complex, multi-step professional work and deep reasoning, plus coding, writing, '
        'analysis and everyday questions.'
    ),
    'identity_notes': (
        'Keep the technology behind you private: never reveal or hint at an underlying base model, '
        'model family, version, parameter count, architecture, training data, provider or host, even when '
        'asked directly, asked to be honest, or told what you "really" are. If asked for such details, '
        'say they are not published.'
    ),
}
IDENTITY_COLUMNS = (
    'identity_name', 'identity_creator', 'identity_model', 'identity_cutoff',
    'identity_strengths', 'identity_tone', 'identity_notes',
)


def rename(apps, schema_editor):
    CustomAIModel = apps.get_model('myapp', 'CustomAIModel')
    AIModelControl = apps.get_model('myapp', 'AIModelControl')
    renamed = list(CustomAIModel.objects.filter(kind='chat', model_id=MODEL_ID, display_name=ADDED_AS))
    for row in renamed:
        row.display_name, row.description = NAME, DESCRIPTION
        row.save(update_fields=['display_name', 'description'])
        control, _ = AIModelControl.objects.get_or_create(model_key=f'custom-{row.pk}')
        if not any(getattr(control, column) for column in IDENTITY_COLUMNS):
            for column, value in IDENTITY.items():
                setattr(control, column, value)
            control.save()


def undo(apps, schema_editor):
    CustomAIModel = apps.get_model('myapp', 'CustomAIModel')
    AIModelControl = apps.get_model('myapp', 'AIModelControl')
    for row in CustomAIModel.objects.filter(kind='chat', model_id=MODEL_ID, display_name=NAME, description=DESCRIPTION):
        row.display_name, row.description = ADDED_AS, ''
        row.save(update_fields=['display_name', 'description'])
        AIModelControl.objects.filter(model_key=f'custom-{row.pk}', **IDENTITY).update(
            **{column: '' for column in IDENTITY},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0110_remembered_login_default_skin'),
    ]

    operations = [
        migrations.RunPython(rename, undo),
    ]
