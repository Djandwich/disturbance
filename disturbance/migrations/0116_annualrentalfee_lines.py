# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2020-07-30 04:18
from __future__ import unicode_literals

import django.contrib.postgres.fields.jsonb
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('disturbance', '0115_merge_20200723_1824'),
    ]

    operations = [
        migrations.AddField(
            model_name='annualrentalfee',
            name='lines',
            field=django.contrib.postgres.fields.jsonb.JSONField(default=['']),
        ),
    ]
