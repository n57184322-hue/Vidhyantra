from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0111_rename_mistral_large_to_gpt_56_sol'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimessage',
            name='video_frames',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
    ]
