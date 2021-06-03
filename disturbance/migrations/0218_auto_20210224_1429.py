# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2021-02-24 06:29
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import multiselectfield.db.fields


class Migration(migrations.Migration):

    dependencies = [
        ('disturbance', '0217_auto_20210205_1529'),
    ]

    operations = [
        migrations.CreateModel(
            name='MasterlistQuestion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('question', models.TextField()),
                ('answer_type', models.CharField(max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='ProposalTypeSection',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('section_name', models.CharField(max_length=100)),
                ('section_label', models.CharField(max_length=100)),
                ('index', models.IntegerField(blank=True, default=0)),
                ('proposal_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='disturbance.ProposalType')),
            ],
        ),
        migrations.CreateModel(
            name='QuestionOption',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='SectionQuestion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tag', multiselectfield.db.fields.MultiSelectField(blank=True, choices=[('isCopiedToPermit', 'isCopiedToPermit')], max_length=400, null=True)),
                ('parent_answer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='disturbance.QuestionOption')),
                ('parent_question', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='children_question', to='disturbance.MasterlistQuestion')),
                ('question', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='question_sections', to='disturbance.MasterlistQuestion')),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='section_questions', to='disturbance.ProposalTypeSection')),
            ],
        ),
        migrations.AddField(
            model_name='masterlistquestion',
            name='option',
            field=models.ManyToManyField(to='disturbance.QuestionOption'),
        ),
    ]
