# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-09-28 05:50
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        #('disturbance', '0003_compliance_post_reminder_sent'),
        ('disturbance', '0005_auto_20181001_1156'),
    ]

    operations = [
        migrations.AlterField(
            model_name='compliance',
            name='requirement',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='compliance_requirement', to='disturbance.ProposalRequirement'),
        ),
        # migrations.AlterField(
        #     model_name='proposaltype',
        #     name='name',
        #     field=models.CharField(choices=[('Disturbance', 'Disturbance'), ('Apiary', 'Apiary')], default='Disturbance', max_length=24, verbose_name='Application name (eg. Disturbance, Apiary)'),
        # ),
    ]
