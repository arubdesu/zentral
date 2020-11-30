# Generated by Django 2.2.17 on 2020-11-30 08:24

from datetime import datetime
from django.db import migrations, models


def set_default_manifest_timestamps(apps, schema_editor):
    now = datetime.utcnow()
    Manifest = apps.get_model("monolith", "Manifest")
    for manifest in Manifest.objects.all():
        manifest.created_at = now
        manifest.updated_at = now
        manifest.save()


class Migration(migrations.Migration):

    dependencies = [
        ('monolith', '0043_auto_20201129_1839'),
    ]

    operations = [
        migrations.AddField(
            model_name='manifest',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        migrations.AddField(
            model_name='manifest',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, null=True),
        ),
        migrations.AddField(
            model_name='manifest',
            name='version',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(set_default_manifest_timestamps),
        migrations.AlterField(
            model_name='manifest',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True),
        ),
        migrations.AlterField(
            model_name='manifest',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
