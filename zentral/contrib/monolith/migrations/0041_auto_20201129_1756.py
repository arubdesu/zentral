# Generated by Django 2.2.17 on 2020-11-29 17:56

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('monolith', '0040_auto_20201012_1432'),
    ]

    operations = [
        migrations.AddField(
            model_name='manifest',
            name='name',
            field=models.CharField(blank=True, max_length=256),
        ),
        migrations.AlterField(
            model_name='manifest',
            name='meta_business_unit',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='inventory.MetaBusinessUnit'),
        ),
    ]
