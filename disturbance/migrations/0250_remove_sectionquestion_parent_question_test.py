# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2021-04-09 06:45
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('disturbance', '0249_sectionquestion_parent_question_test'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='sectionquestion',
            name='parent_question_test',
        ),
    ]
