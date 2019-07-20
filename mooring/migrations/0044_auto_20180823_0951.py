# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-08-23 01:51
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('mooring', '0043_admissionsbooking_noofconcessions'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='admissionsbooking',
            name='email',
        ),
        migrations.RemoveField(
            model_name='admissionsbooking',
            name='expiryTime',
        ),
        migrations.RemoveField(
            model_name='admissionsbooking',
            name='name',
        ),
        migrations.AddField(
            model_name='admissionsbooking',
            name='customer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='admissionsbooking',
            name='vesselRegNo',
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
