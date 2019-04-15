# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2019-04-01 14:23
from __future__ import unicode_literals

import commercialoperator.components.compliances.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('commercialoperator', '0065_auto_20190401_1443'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='proposal',
            name='other_details',
        ),
        migrations.AddField(
            model_name='proposalotherdetails',
            name='proposal',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='other_details', to='commercialoperator.Proposal'),
        ),
        migrations.AlterField(
            model_name='compliancedocument',
            name='_file',
            field=models.FileField(upload_to=commercialoperator.components.compliances.models.update_proposal_complaince_filename),
        ),
    ]
