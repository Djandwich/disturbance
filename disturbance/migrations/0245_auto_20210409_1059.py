# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2021-04-09 02:59
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('disturbance', '0244_auto_20210409_1055'),
    ]

    operations = [
        migrations.AlterField(
            model_name='masterlistquestion',
            name='help_text',
            field=models.TextField(blank=True, null=True, verbose_name='Help text 2'),
        ),
        migrations.AlterField(
            model_name='masterlistquestion',
            name='help_text_assessor',
            field=models.TextField(blank=True, null=True, verbose_name='Help text assessor2'),
        ),
    ]
