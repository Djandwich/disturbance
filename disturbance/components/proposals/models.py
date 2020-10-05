from __future__ import unicode_literals

import json
import datetime

import pytz
import requests
from django.contrib.gis.db.models.fields import PointField
from django.contrib.gis.db.models.manager import GeoManager
from django.contrib.gis.geos import GEOSGeometry
from django.contrib.gis.measure import Distance
from django.db import models,transaction
from django.contrib.gis.db import models as gis_models
from django.db.models import Q
from django.dispatch import receiver
from django.db.models.signals import pre_delete, post_save
from django.utils.encoding import python_2_unicode_compatible
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields.jsonb import JSONField
from django.utils import timezone
from ledger.settings_base import TIME_ZONE
from rest_framework import serializers
from taggit.models import TaggedItemBase
from ledger.accounts.models import EmailUser, RevisionedMixin
from ledger.payments.models import Invoice
from disturbance import exceptions
# from disturbance.components.approvals.models import ApiarySiteOnApproval
from disturbance.components.organisations.models import Organisation
from disturbance.components.main.models import CommunicationsLogEntry, UserAction, Document, Region, District, \
    ApplicationType, RegionDbca, DistrictDbca, CategoryDbca
from disturbance.components.main.utils import get_department_user
from disturbance.components.proposals.email import (
        send_referral_email_notification, 
        send_apiary_referral_email_notification, 
        send_apiary_referral_complete_email_notification, 
        send_proposal_decline_email_notification,
        send_proposal_approval_email_notification, 
        send_amendment_email_notification,
        send_submit_email_notification, 
        send_external_submit_email_notification, 
        send_approver_decline_email_notification, 
        send_approver_approve_email_notification, 
        send_referral_complete_email_notification, 
        send_proposal_approver_sendback_email_notification, 
        send_referral_recall_email_notification,
        send_site_transfer_approval_email_notification,
        )
from disturbance.ordered_model import OrderedModel
import copy
import subprocess

import logging

from disturbance.settings import SITE_STATUS_DRAFT, SITE_STATUS_PENDING, SITE_STATUS_APPROVED, SITE_STATUS_DENIED, \
    SITE_STATUS_CURRENT, RESTRICTED_RADIUS, SITE_STATUS_TRANSFERRED

logger = logging.getLogger(__name__)


def update_proposal_doc_filename(instance, filename):
    return 'proposals/{}/documents/{}'.format(instance.proposal.id,filename)

def update_proposal_comms_log_filename(instance, filename):
    return 'proposals/{}/communications/{}/{}'.format(instance.log_entry.proposal.id,instance.id,filename)

def update_amendment_request_doc_filename(instance, filename):
    return 'proposals/{}/amendment_request_documents/{}'.format(instance.amendment_request.proposal.id,filename)

def update_apiary_doc_filename(instance, filename):
    return 'proposals/{}/apiary_documents/{}'.format(instance.apiary_documents.proposal.id, filename)

#def update_temporary_use_doc_filename(instance, filename):
#    return 'proposals/{}/apiary_temporary_use_documents/{}'.format(instance.apiary_temporary_use.proposal.id, filename)
#
#def update_site_transfer_doc_filename(instance, filename):
#    return 'proposals/{}/apiary_site_transfer_documents/{}'.format(instance.apiary_site_transfer.proposal.id, filename)


def application_type_choicelist():
    try:
        #return [( (choice.name), (choice.name) ) for choice in ApplicationType.objects.filter(visible=True)]
        return [( (choice.name), (choice.name) ) for choice in ApplicationType.objects.all()]
    except:
        # required because on first DB tables creation, there are no ApplicationType objects -- setting a default value
        return ( ('Disturbance', 'Disturbance'), )

class ProposalType(models.Model):
    #name = models.CharField(verbose_name='Application name (eg. Disturbance, Apiary)', max_length=24)
    #application_type = models.ForeignKey(ApplicationType, related_name='aplication_types')
    description = models.CharField(max_length=256, blank=True, null=True)
    #name = models.CharField(verbose_name='Application name (eg. Disturbance, Apiary)', max_length=24, choices=application_type_choicelist(), default=application_type_choicelist()[0][0])
    name = models.CharField(verbose_name='Application name (eg. Disturbance, Apiary)', max_length=64, choices=application_type_choicelist(), default='Disturbance')
    schema = JSONField()
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.")
    #site = models.OneToOneField(Site, default='1')
    replaced_by = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    def __str__(self):
        return '{} - v{}'.format(self.name, self.version)

    class Meta:
        app_label = 'disturbance'
        unique_together = ('name', 'version')


class TaggedProposalAssessorGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'disturbance'

class TaggedProposalAssessorGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'disturbance'

class ProposalAssessorGroup(models.Model):
    name = models.CharField(max_length=255)
    #members = models.ManyToManyField(EmailUser,blank=True)
    #regions = TaggableManager(verbose_name="Regions",help_text="A comma-separated list of regions.",through=TaggedProposalAssessorGroupRegions,related_name = "+",blank=True)
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.",through=TaggedProposalAssessorGroupActivities,related_name = "+",blank=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalAssessorGroup.objects.get(default=True)
        except ProposalAssessorGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')
#            elif default and not self.default:
#                raise ValidationError('There can only be one default proposal assessor group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal assessor group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_officer == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_assessor','with_referral','with_assessor_requirements']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class TaggedProposalApproverGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'disturbance'

class TaggedProposalApproverGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'disturbance'

class ProposalApproverGroup(models.Model):
    name = models.CharField(max_length=255)
    #members = models.ManyToManyField(EmailUser,blank=True)
    #regions = TaggableManager(verbose_name="Regions",help_text="A comma-separated list of regions.",through=TaggedProposalApproverGroupRegions,related_name = "+",blank=True)
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.",through=TaggedProposalApproverGroupActivities,related_name = "+",blank=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalApproverGroup.objects.get(default=True)
        except ProposalApproverGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')

#            if int(self.pk) != int(default.id):
#                if default and self.default:
#                    raise ValidationError('There can only be one default proposal approver group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal approver group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_approver == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_approver']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class DefaultDocument(Document):
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    class Meta:
        app_label = 'disturbance'
        abstract =True

    def delete(self):
        if self.can_delete:
            return super(DefaultDocument, self).delete()
        logger.info('Cannot delete existing document object after Application has been submitted (including document submitted before Application pushback to status Draft): {}'.format(self.name))


class ProposalDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_doc_filename, max_length=500)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    can_hide= models.BooleanField(default=False) # after initial submit, document cannot be deleted but can be hidden
    hidden=models.BooleanField(default=False) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'disturbance'


class Proposal(RevisionedMixin):
    CUSTOMER_STATUS_TEMP = 'temp'
    CUSTOMER_STATUS_DRAFT = 'draft'
    CUSTOMER_STATUS_WITH_ASSESSOR = 'with_assessor'
    CUSTOMER_STATUS_AMENDMENT_REQUEST = 'amendment_required'
    CUSTOMER_STATUS_APPROVED = 'approved'
    CUSTOMER_STATUS_DECLINED = 'declined'
    CUSTOMER_STATUS_DISCARDED = 'discarded'
    CUSTOMER_STATUS_CHOICES = ((CUSTOMER_STATUS_TEMP, 'Temporary'),
                               (CUSTOMER_STATUS_DRAFT, 'Draft'),
                               (CUSTOMER_STATUS_WITH_ASSESSOR, 'Under Review'),
                               (CUSTOMER_STATUS_AMENDMENT_REQUEST, 'Amendment Required'),
                               (CUSTOMER_STATUS_APPROVED, 'Approved'),
                               (CUSTOMER_STATUS_DECLINED, 'Declined'),
                               (CUSTOMER_STATUS_DISCARDED, 'Discarded'),
                               )
    # List of statuses from above that allow a customer to edit an application.
    CUSTOMER_EDITABLE_STATE = [CUSTOMER_STATUS_TEMP, CUSTOMER_STATUS_DRAFT, CUSTOMER_STATUS_AMENDMENT_REQUEST, ]

    APPLICANT_TYPE_ORGANISATION = 'organisation'
    APPLICANT_TYPE_PROXY = 'proxy' # proxy also represents an individual making an Apiary application
    APPLICANT_TYPE_SUBMITTER = 'submitter'

    # List of statuses from above that allow a customer to view an application (read-only)
    CUSTOMER_VIEWABLE_STATE = ['with_assessor', 'under_review', 'id_required', 'returns_required', 'approved', 'declined']

    PROCESSING_STATUS_TEMP = 'temp'
    PROCESSING_STATUS_DRAFT = 'draft'
    PROCESSING_STATUS_WITH_ASSESSOR = 'with_assessor'
    PROCESSING_STATUS_WITH_REFERRAL = 'with_referral'
    PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS = 'with_assessor_requirements'
    PROCESSING_STATUS_WITH_APPROVER = 'with_approver'
    PROCESSING_STATUS_RENEWAL = 'renewal'
    PROCESSING_STATUS_LICENCE_AMENDMENT = 'licence_amendment'
    PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE = 'awaiting_applicant_response'
    PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE = 'awaiting_assessor_response'
    PROCESSING_STATUS_AWAITING_RESPONSES = 'awaiting_responses'
    PROCESSING_STATUS_READY_FOR_CONDITIONS = 'ready_for_conditions'
    PROCESSING_STATUS_READY_TO_ISSUE = 'ready_to_issue'
    PROCESSING_STATUS_APPROVED = 'approved'
    PROCESSING_STATUS_DECLINED = 'declined'
    PROCESSING_STATUS_DISCARDED = 'discarded'
    PROCESSING_STATUS_CHOICES = ((PROCESSING_STATUS_TEMP, 'Temporary'),
                                 (PROCESSING_STATUS_DRAFT, 'Draft'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR, 'With Assessor'),
                                 (PROCESSING_STATUS_WITH_REFERRAL, 'With Referral'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS, 'With Assessor (Requirements)'),
                                 (PROCESSING_STATUS_WITH_APPROVER, 'With Approver'),
                                 (PROCESSING_STATUS_RENEWAL, 'Renewal'),
                                 (PROCESSING_STATUS_LICENCE_AMENDMENT, 'Licence Amendment'),
                                 (PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE, 'Awaiting Applicant Response'),
                                 (PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE, 'Awaiting Assessor Response'),
                                 (PROCESSING_STATUS_AWAITING_RESPONSES, 'Awaiting Responses'),
                                 (PROCESSING_STATUS_READY_FOR_CONDITIONS, 'Ready for Conditions'),
                                 (PROCESSING_STATUS_READY_TO_ISSUE, 'Ready to Issue'),
                                 (PROCESSING_STATUS_APPROVED, 'Approved'),
                                 (PROCESSING_STATUS_DECLINED, 'Declined'),
                                 (PROCESSING_STATUS_DISCARDED, 'Discarded'),
                                 )

    ID_CHECK_STATUS_CHOICES = (('not_checked', 'Not Checked'), ('awaiting_update', 'Awaiting Update'),
                               ('updated', 'Updated'), ('accepted', 'Accepted'))

    COMPLIANCE_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('awaiting_returns', 'Awaiting Returns'), ('completed', 'Completed'),
        ('accepted', 'Accepted'))

    CHARACTER_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('accepted', 'Accepted'))

    REVIEW_STATUS_CHOICES = (
        ('not_reviewed', 'Not Reviewed'), ('awaiting_amendments', 'Awaiting Amendments'), ('amended', 'Amended'),
        ('accepted', 'Accepted'))

#    PROPOSAL_STATE_NEW_LICENCE = 'New Licence'
#    PROPOSAL_STATE_AMENDMENT = 'Amendment'
#    PROPOSAL_STATE_RENEWAL = 'Renewal'
#    PROPOSAL_STATE_CHOICES = (
#        (1, PROPOSAL_STATE_NEW_LICENCE),
#        (2, PROPOSAL_STATE_AMENDMENT),
#        (3, PROPOSAL_STATE_RENEWAL),
#    )

    APPLICATION_TYPE_CHOICES = (
        ('new_proposal', 'New Proposal'),
        ('amendment', 'Amendment'),
        ('renewal', 'Renewal'),
    )

    proposal_type = models.CharField('Proposal Type', max_length=40, choices=APPLICATION_TYPE_CHOICES,
                                        default=APPLICATION_TYPE_CHOICES[0][0])
    #proposal_state = models.PositiveSmallIntegerField('Proposal state', choices=PROPOSAL_STATE_CHOICES, default=1)

    data = JSONField(blank=True, null=True)
    assessor_data = JSONField(blank=True, null=True)
    comment_data = JSONField(blank=True, null=True)
    schema = JSONField(blank=False, null=False)
    proposed_issuance_approval = JSONField(blank=True, null=True)
    #hard_copy = models.ForeignKey(Document, blank=True, null=True, related_name='hard_copy')

    customer_status = models.CharField('Customer Status', max_length=40, choices=CUSTOMER_STATUS_CHOICES,
                                       default=CUSTOMER_STATUS_CHOICES[1][0])
    applicant = models.ForeignKey(Organisation, blank=True, null=True, related_name='proposals')

    lodgement_number = models.CharField(max_length=9, blank=True, default='')
    lodgement_sequence = models.IntegerField(blank=True, default=0)
    #lodgement_date = models.DateField(blank=True, null=True)
    lodgement_date = models.DateTimeField(blank=True, null=True)
    # 20200512 - proxy_applicant also represents an individual making an Apiary application
    proxy_applicant = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proxy')
    submitter = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals')

    assigned_officer = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals_assigned', on_delete=models.SET_NULL)
    assigned_approver = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals_approvals', on_delete=models.SET_NULL)
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[1][0])
    id_check_status = models.CharField('Identification Check Status', max_length=30, choices=ID_CHECK_STATUS_CHOICES,
                                       default=ID_CHECK_STATUS_CHOICES[0][0])
    compliance_check_status = models.CharField('Return Check Status', max_length=30, choices=COMPLIANCE_CHECK_STATUS_CHOICES,
                                            default=COMPLIANCE_CHECK_STATUS_CHOICES[0][0])
    character_check_status = models.CharField('Character Check Status', max_length=30,
                                              choices=CHARACTER_CHECK_STATUS_CHOICES,
                                              default=CHARACTER_CHECK_STATUS_CHOICES[0][0])
    review_status = models.CharField('Review Status', max_length=30, choices=REVIEW_STATUS_CHOICES,
                                     default=REVIEW_STATUS_CHOICES[0][0])

    approval = models.ForeignKey('disturbance.Approval',null=True,blank=True)

    previous_application = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    proposed_decline_status = models.BooleanField(default=False)
    # Special Fields
    title = models.CharField(max_length=255,null=True,blank=True)
    activity = models.CharField(max_length=255,null=True,blank=True)
    #region = models.CharField(max_length=255,null=True,blank=True)
    tenure = models.CharField(max_length=255,null=True,blank=True)
    #activity = models.ForeignKey(Activity, null=True, blank=True)
    region = models.ForeignKey(Region, null=True, blank=True)
    district = models.ForeignKey(District, null=True, blank=True)
    #tenure = models.ForeignKey(Tenure, null=True, blank=True)
    application_type = models.ForeignKey(ApplicationType)
    approval_level = models.CharField('Activity matrix approval level', max_length=255,null=True,blank=True)
    approval_level_document = models.ForeignKey(ProposalDocument, blank=True, null=True, related_name='approval_level_document')
    approval_level_comment = models.TextField(blank=True)
    approval_comment = models.TextField(blank=True)
    assessment_reminder_sent = models.BooleanField(default=False)
    sub_activity_level1 = models.CharField(max_length=255,null=True,blank=True)
    sub_activity_level2 = models.CharField(max_length=255,null=True,blank=True)
    management_area = models.CharField(max_length=255,null=True,blank=True)

    fee_invoice_reference = models.CharField(max_length=50, null=True, blank=True, default='')
    migrated = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'
        #ordering = ['-id']

    def __str__(self):
        return str(self.id)

    #Append 'P' to Proposal id to generate Lodgement number. Lodgement number and lodgement sequence are used to generate Reference.
    def save(self, *args, **kwargs):
        super(Proposal, self).save(*args,**kwargs)
        if self.lodgement_number == '':
            new_lodgment_id = 'P{0:06d}'.format(self.pk)
            self.lodgement_number = new_lodgment_id
            self.save()

    @property
    def fee_paid(self):
        return True if self.fee_invoice_reference or self.proposal_type=='amendment' else False

    @property
    def fee_amount(self):
        return Invoice.objects.get(reference=self.fee_invoice_reference).amount if self.fee_paid else None

    @property
    def relevant_applicant(self):
        if self.applicant:
            return self.applicant
        elif self.proxy_applicant:
            return self.proxy_applicant
        else:
            return self.submitter

    @property
    def relevant_applicant_name(self):
        if self.applicant:
            return self.applicant.name
        elif self.proxy_applicant:
            return self.proxy_applicant.get_full_name()
        else:
            return self.submitter.get_full_name()

    @property
    def relevant_applicant_description(self):
        if self.applicant:
            return self.applicant.organisation.name
        elif self.proxy_applicant:
            return "{} {}".format(
                self.proxy_applicant.first_name,
                self.proxy_applicant.last_name)
        else:
            return "{} {}".format(
                self.submitter.first_name,
                self.submitter.last_name)

    @property
    def relevant_applicant_email(self):
        if self.applicant and hasattr(self.applicant.organisation, 'email') and self.applicant.organisation.email:
            return self.applicant.organisation.email
        elif self.proxy_applicant:
            return self.proxy_applicant.email
        else:
            return self.submitter.email

    @property
    def relevant_applicant_details(self):
        if self.applicant:
            return '{} \n{}'.format(
                self.applicant.organisation.name,
                self.applicant.address)
        elif self.proxy_applicant:
            return "{} {}\n{}".format(
                self.proxy_applicant.first_name,
                self.proxy_applicant.last_name,
                self.proxy_applicant.addresses.all().first())
        else:
            return "{} {}\n{}".format(
                self.submitter.first_name,
                self.submitter.last_name,
                self.submitter.addresses.all().first())

    @property
    def relevant_applicant_address(self):
        if self.applicant:
            return self.applicant.address
        elif self.proxy_applicant:
            #return self.proxy_applicant.addresses.all().first()
            return self.proxy_applicant.residential_address
        else:
            #return self.submitter.addresses.all().first()
            return self.submitter.residential_address

    @property
    def relevant_applicant_id(self):
        return_value = None
        if self.applicant:
            print("APPLICANT")
            return_value = self.applicant.id
        elif self.proxy_applicant:
            print("PROXY_APPLICANT")
            return_value = self.proxy_applicant.id
        else:
            #return_value = self.submitter.id
            pass
        return return_value

    @property
    def relevant_applicant_type(self):
        if self.applicant:
            return self.APPLICANT_TYPE_ORGANISATION
        elif self.proxy_applicant:
            return self.APPLICANT_TYPE_PROXY
        else:
            return self.APPLICANT_TYPE_SUBMITTER

    @property
    def applicant_field(self):
        if self.applicant:
            return 'applicant'
        elif self.proxy_applicant:
            return 'proxy_applicant'
        else:
            return 'submitter'


    @property
    def reference(self):
        return '{}-{}'.format(self.lodgement_number, self.lodgement_sequence)

    @property
    def get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( dict(id=p.previous_application.id, modified=p.previous_application.modified_date) )
            p = p.previous_application
        return l


    def _get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( [p.id, p.previous_application.id] )
            p = p.previous_application
        return l

    @property
    def is_assigned(self):
        return self.assigned_officer is not None

    @property
    def is_temporary(self):
        return self.customer_status == 'temp' and self.processing_status == 'temp'

    @property
    def can_user_edit(self):
        """
        :return: True if the application is in one of the editable status.
        """
        return self.customer_status in self.CUSTOMER_EDITABLE_STATE

    @property
    def can_user_view(self):
        """
        :return: True if the application is in one of the approved status.
        """
        return self.customer_status in self.CUSTOMER_VIEWABLE_STATE



    @property
    def is_discardable(self):
        """
        An application can be discarded by a customer if:
        1 - It is a draft
        2- or if the application has been pushed back to the user
        """
        return self.customer_status == 'draft' or self.processing_status == 'awaiting_applicant_response'

    @property
    def is_deletable(self):
        """
        An application can be deleted only if it is a draft and it hasn't been lodged yet
        :return:
        """
        return self.customer_status == 'draft' and not self.lodgement_number

    @property
    def latest_referrals(self):
        return self.referrals.all()[:2]

    @property
    def regions_list(self):
        #return self.region.split(',') if self.region else []
        return [self.region.name] if self.region else []

    @property
    def permit(self):
        return self.approval.licence_document._file.url if self.approval else None

    @property
    def allowed_assessors(self):
        if self.processing_status == 'with_approver':
            group = self.__approver_group()
        else:
            group = self.__assessor_group()
        return group.members.all() if group else []

    #Compliance and Approvals use assessor group to show/hide compliance/approvals actions on dashboard
    @property
    def compliance_assessors(self):
        group = self.__assessor_group()
        return group.members.all() if group else []

    #Approver group required to show/hide reissue actions on Approval dashboard
    @property
    def allowed_approvers(self):
        group = self.__approver_group()
        return group.members.all() if group else []



    @property
    def can_officer_process(self):
        """
        :return: True if the application is in one of the processable status for Assessor role.
        """
        officer_view_state = ['draft','approved','declined','temp','discarded']
        if self.processing_status in officer_view_state:
            return False
        else:
            return True

    @property
    def amendment_requests(self):
        qs =AmendmentRequest.objects.filter(proposal = self)
        return qs

    @property
    def apiary_group_application_type(self):
        apiary = False
        if self.application_type and self.application_type.name in (
                ApplicationType.APIARY,
                ApplicationType.TEMPORARY_USE,
                ApplicationType.SITE_TRANSFER,
                ):
            apiary = True
        return apiary

    def __assessor_group(self):
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryAssessorGroup.objects.first()
            if group:
                return group
        # TODO get list of assessor groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalAssessorGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalAssessorGroup.DoesNotExist:
                pass
        default_group = ProposalAssessorGroup.objects.get(default=True)

        return default_group


    def __approver_group(self):
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryApproverGroup.objects.first()
            if group:
                return group
        # TODO get list of approver groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalApproverGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalApproverGroup.DoesNotExist:
                pass
        default_group = ProposalApproverGroup.objects.get(default=True)

        return default_group

    def __check_proposal_filled_out(self):
        if not self.data:
            raise exceptions.ProposalNotComplete()
        missing_fields = []
        required_fields = {
            'region':'Region/District',
        #    'title': 'Title',
        #    'activity': 'Activity'
        }
        #import ipdb; ipdb.set_trace()
        for k,v in required_fields.items():
            val = getattr(self,k)
            if not val:
                missing_fields.append(v)
        return missing_fields

    @property
    def assessor_recipients(self):
        recipients = []
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryAssessorGroup.objects.first()
            if group:
                return group.members_email
        #import ipdb; ipdb.set_trace()
        # Proposal logic
        try:
            recipients = ProposalAssessorGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalAssessorGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients

    @property
    def approver_recipients(self):
        recipients = []
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryApproverGroup.objects.first()
            if group:
                return group.members_email
        # Proposal logic
        try:
            recipients = ProposalApproverGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalApproverGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients

    @property
    def hasAmendmentRequest(self):
        qs = self.amendment_requests
        qs = qs.filter(status = 'requested')
        if qs:
            return True
        return False


    def referral_email_list(self,user):
        qs=self.referrals.all()
        email_list=[]
        if self.assigned_officer:
            email_list.append(self.assigned_officer.email)
        else:
            email_list.append(user.email)
        if qs:
            for r in qs:
                email_list.append(r.referral.email)
        separator=', '
        email_list_string=separator.join(email_list)
        return email_list_string



    def can_assess(self,user):
        if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements':
            if self.apiary_group_application_type:
                # Apiary logic
                return self.__assessor_group() in user.apiaryassessorgroup_set.all()
            else:
                # Proposal logic
                return self.__assessor_group() in user.proposalassessorgroup_set.all()
        elif self.processing_status == 'with_approver':
            if self.apiary_group_application_type:
                # Apiary logic
                return self.__approver_group() in user.apiaryapprovergroup_set.all()
            else:
                # Proposal logic
                return self.__approver_group() in user.proposalapprovergroup_set.all()
        else:
            return False

    def assessor_comments_view(self,user):

        if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements' or self.processing_status == 'with_approver':
            try:
                referral = Referral.objects.get(proposal=self,referral=user)
            except:
                referral = None
            if referral:
                return True
            elif self.__assessor_group() in user.proposalassessorgroup_set.all():
                return True
            elif self.__approver_group() in user.proposalapprovergroup_set.all():
                return True
            else:
                return False
        else:
            return False

    def has_assessor_mode(self,user):
        status_without_assessor = ['with_approver','approved','declined','draft']
        if self.processing_status in status_without_assessor:
            return False
        else:
            if self.assigned_officer:
                if self.assigned_officer == user:
                    if self.apiary_group_application_type:
                        # Apiary logic
                        return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                    else:
                        # Proposal logic
                        return self.__assessor_group() in user.proposalassessorgroup_set.all()
                else:
                    return False
            else:
                if self.apiary_group_application_type:
                    # Apiary logic
                    return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                else:
                    # Proposal logic
                    return self.__assessor_group() in user.proposalassessorgroup_set.all()

    def log_user_action(self, action, request):
        return ProposalUserAction.log_action(self, action, request.user)

    def submit(self,request,viewset):
        from disturbance.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                #import ipdb; ipdb.set_trace()
                if self.application_type.name != ApplicationType.APIARY:
                    # Check if the special fields have been completed
                    missing_fields = self.__check_proposal_filled_out()
                    if missing_fields:
                        error_text = 'The proposal has these missing fields, {}'.format(','.join(missing_fields))
                        raise exceptions.ProposalMissingFields(detail=error_text)
                self.submitter = request.user
                #self.lodgement_date = datetime.datetime.strptime(timezone.now().strftime('%Y-%m-%d'),'%Y-%m-%d').date()
                self.lodgement_date = timezone.now()
                if (self.amendment_requests):
                    qs = self.amendment_requests.filter(status = "requested")
                    if (qs):
                        for q in qs:
                            q.status = 'amended'
                            q.save()

                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.id),request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.id),request)

                #import ipdb; ipdb.set_trace()
                ret1 = send_submit_email_notification(request, self)
                ret2 = send_external_submit_email_notification(request, self)

                if ret1 and ret2:
                    self.processing_status = 'with_assessor'
                    self.customer_status = 'with_assessor'
                    self.documents.all().update(can_delete=False)
                    self.save()
                else:
                    raise ValidationError('An error occurred while submitting proposal (Submit email notifications failed)')
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')
        return self

    def update(self,request,viewset):
        from disturbance.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            #import ipdb; ipdb.set_trace()
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                self.save()
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')


    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                referral_email = referral_email.lower()
                if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral':
                    self.processing_status = 'with_referral'
                    self.save()
                    referral = None

                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email)
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    try:
                        Referral.objects.get(referral=user,proposal=self)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self,
                            referral=user,
                            sent_by=request.user,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # Create a log entry for the organisation
                    if self.applicant:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # send email
                    send_referral_email_notification(referral,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    def assign_officer(self,request,officer):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if not self.can_assess(officer):
                    raise ValidationError('The selected person is not authorised to be assigned to this proposal')
                if self.processing_status == 'with_approver':
                    if officer != self.assigned_approver:
                        self.assigned_approver = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                else:
                    if officer != self.assigned_officer:
                        self.assigned_officer = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
            except:
                raise

    def assing_approval_level_document(self, request):
        with transaction.atomic():
            try:
                approval_level_document = request.data['approval_level_document']
                if approval_level_document != 'null':
                    try:
                        document = self.documents.get(input_name=str(approval_level_document))
                    except ProposalDocument.DoesNotExist:
                        document = self.documents.get_or_create(input_name=str(approval_level_document), name=str(approval_level_document))[0]
                    document.name = str(approval_level_document)
                    # commenting out below tow lines - we want to retain all past attachments - reversion can use them
                    #if document._file and os.path.isfile(document._file.path):
                    #    os.remove(document._file.path)
                    document._file = approval_level_document
                    document.save()
                    d=ProposalDocument.objects.get(id=document.id)
                    self.approval_level_document = d
                    comment = 'Approval Level Document Added: {}'.format(document.name)
                else:
                    self.approval_level_document = None
                    comment = 'Approval Level Document Deleted: {}'.format(request.data['approval_level_document_name'])
                #self.save()
                self.save(version_comment=comment) # to allow revision to be added to reversion history
                self.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.id),request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.id),request)
                return self
            except:
                raise

    def save_approval_level_comment(self, request):
        with transaction.atomic():
            try:
                approval_level_comment = request.data['approval_level_comment']
                self.approval_level_comment=approval_level_comment
                self.save()
                self.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_COMMENT.format(self.id),request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_COMMENT.format(self.id),request)
                return self
            except:
                raise

    def unassign(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status == 'with_approver':
                    if self.assigned_approver:
                        self.assigned_approver = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.id),request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.id),request)
                else:
                    if self.assigned_officer:
                        self.assigned_officer = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.id),request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.id),request)
            except:
                raise

    def move_to_status(self,request,status, approver_comment):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if status in ['with_assessor','with_assessor_requirements','with_approver']:
            if self.processing_status == 'with_referral' or self.can_user_edit:
                raise ValidationError('You cannot change the current status at this time')
            if self.processing_status != status:
                if self.processing_status =='with_approver':
                    if approver_comment:
                        self.approver_comment = approver_comment
                        self.save()
                        send_proposal_approver_sendback_email_notification(request, self)
                self.processing_status = status
                self.save()

                # Create a log entry for the proposal
                if self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR:
                    self.log_user_action(ProposalUserAction.ACTION_BACK_TO_PROCESSING.format(self.id),request)
                elif self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS:
                    self.log_user_action(ProposalUserAction.ACTION_ENTER_REQUIREMENTS.format(self.id),request)
        else:
            raise ValidationError('The provided status cannot be found.')

    def reissue_approval(self,request,status):
        if not self.processing_status=='approved' :
            raise ValidationError('You cannot change the current status at this time')
        elif self.approval and self.approval.can_reissue:
            # Apiary logic in first condition
            if self.apiary_group_application_type and self.__approver_group() in request.user.apiaryapprovergroup_set.all():
                self.processing_status = status
                self.save()
                self.approval.reissued=True
                self.approval.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_REISSUE_APPROVAL.format(self.id),request)
            elif self.__approver_group() in request.user.proposalapprovergroup_set.all():
                self.processing_status = status
                self.save()
                self.approval.reissued=True
                self.approval.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_REISSUE_APPROVAL.format(self.id),request)
            else:
                raise ValidationError('Cannot reissue Approval')
        else:
            raise ValidationError('Cannot reissue Approval')

    def proposed_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor':
                    raise ValidationError('You cannot propose to decline if it is not with assessor')

                reason = details.get('reason')
                ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer': request.user, 'reason': reason, 'cc_email': details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.id),request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.id),request)

                send_approver_decline_email_notification(reason, request, self)
            except:
                raise

    def final_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot decline if it is not with approver')

                proposal_decline, success = ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer':request.user,'reason':details.get('reason'),'cc_email':details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                self.processing_status = 'declined'
                self.customer_status = 'declined'
                self.save()

                if self.proposal_apiary:
                    # Update apiary site status
                    self.proposal_apiary.final_decline()

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id),request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id),request)
                send_proposal_decline_email_notification(self,request, proposal_decline)
            except:
                raise

    def preview_approval(self,request,details):
        from disturbance.components.approvals.models import PreviewTempApproval
        from disturbance.components.approvals.models import Approval
        with transaction.atomic():
            try:
                if self.processing_status != 'with_approver':
                    raise ValidationError('Licence preview only available when processing status is with_approver. Current status {}'.format(self.processing_status))
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                #if not self.applicant.organisation.postal_address:
                if not self.relevant_applicant_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal.')

                lodgement_number = self.previous_application.approval.lodgement_number if self.proposal_type in ['renewal', 'amendment'] else '' # renewals/amendments keep same licence number
                # Apiary Site Transfer logic
                form_data_str = request.POST.get('formData')
                form_data = json.loads(form_data_str)
                #if isinstance(form_data, list):
                originating_approval_id = form_data.get('originating_approval_id')
                target_approval_id = form_data.get('target_approval_id')
                licence_buffer = None
                if originating_approval_id:
                    preview_approval = Approval.objects.get(id=originating_approval_id)
                    licence_buffer = preview_approval.generate_apiary_site_transfer_doc(
                            request.user, 
                            site_transfer_proposal=self,
                            preview=True
                            )
                elif target_approval_id:
                    preview_approval = Approval.objects.get(id=target_approval_id)
                    licence_buffer = preview_approval.generate_apiary_site_transfer_doc(
                            request.user, 
                            site_transfer_proposal=self,
                            preview=True
                            )
                # All other logic
                else:
                    preview_approval = PreviewTempApproval.objects.create(
                        current_proposal = self,
                        issue_date = timezone.now(),
                        expiry_date = datetime.datetime.strptime(details.get('due_date'), '%d/%m/%Y').date(),
                        start_date = datetime.datetime.strptime(details.get('start_date'), '%d/%m/%Y').date(),
                        #submitter = self.submitter,
                        #org_applicant = self.applicant if isinstance(self.applicant, Organisation) else None,
                        #proxy_applicant = self.applicant if isinstance(self.applicant, EmailUser) else None,
                        applicant = self.applicant,
                        proxy_applicant = self.proxy_applicant,
                        lodgement_number = lodgement_number,
                        apiary_approval = self.apiary_group_application_type,
                    )

                    # Generate the preview document - get the value of the BytesIO buffer
                    licence_buffer = preview_approval.generate_doc(request.user, preview=True)

                    # clean temp preview licence object
                    transaction.set_rollback(True)

                return licence_buffer
            except:
                raise

    def proposed_approval(self,request,details):
        with transaction.atomic():
            #import ipdb; ipdb.set_trace()
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor_requirements':
                    raise ValidationError('You cannot propose for approval if it is not with assessor for requirements')
                # Do not accept new start and expiry dates for Apiary group applications with a licence, unless the licence has been reissued
                start_date = details.get('start_date').strftime('%d/%m/%Y') if details.get('start_date') else None
                expiry_date = details.get('expiry_date').strftime('%d/%m/%Y') if details.get('expiry_date') else None
                if self.apiary_group_application_type:
                    if self.approval and (self.approval.reissued or self.proposal_type == 'renewal'):
                        self.proposed_issuance_approval = {
                            'start_date' : start_date,
                            'expiry_date' : expiry_date,
                            'details' : details.get('details'),
                            'cc_email' : details.get('cc_email'),
                        }
                    elif self.proposed_issuance_approval:
                        self.proposed_issuance_approval = {
                                #'start_date' : self.proposed_issuance_approval.get('start_date') if self.proposed_issuance_approval.get('start_date') else details.get('start_date').strftime('%d/%m/%Y'),
                                #'expiry_date' : self.proposed_issuance_approval.get('expiry_date') if self.proposed_issuance_approval.get('expiry_date') else details.get('expiry_date').strftime('%d/%m/%Y'),
                                'details' : details.get('details'),
                                'cc_email' : details.get('cc_email'),
                        }
                    else:
                        self.proposed_issuance_approval = {
                                'start_date' : start_date,
                                'expiry_date' : expiry_date,
                                'details' : details.get('details'),
                                'cc_email' : details.get('cc_email'),
                        }
                # non-apiary Proposals
                else:
                    self.proposed_issuance_approval = {
                            'start_date' : start_date,
                            'expiry_date' : expiry_date,
                            'details' : details.get('details'),
                            'cc_email' : details.get('cc_email'),
                    }

                self.proposed_decline_status = False
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                self.assigned_officer = None

                apiary_sites = request.data.get('apiary_sites', None)
                apiary_sites_list = []
                if apiary_sites:
                    # When new apiary proposal
                    if self.application_type.name == ApplicationType.APIARY:
                        for apiary_site in apiary_sites:
                            my_site = ApiarySite.objects.get(id=apiary_site['id'])
                            # my_site.workflow_selected_status = apiary_site['checked']
                            self.proposal_apiary.set_workflow_selected_status(my_site, apiary_site.get('checked'))
                            if apiary_site.get('checked'):
                                apiary_sites_list.append(apiary_site.get('id'))
                            # my_site.save()

                            if apiary_site.get('checked') and 'coordinates_moved' in apiary_site:
                                relation = self.proposal_apiary.get_relation(my_site)
                                prev_coordinates = relation.wkb_geometry_processed.get_coords()

                                # Update coordinate (Assessor and Approver can move the proposed site location)
                                geom_str = GEOSGeometry('POINT(' + str(apiary_site['coordinates_moved']['lng']) + ' ' + str(apiary_site['coordinates_moved']['lat']) + ')', srid=4326)
                                # from disturbance.components.proposals.serializers_apiary import ApiarySiteSavePointPendingSerializer
                                # serializer = ApiarySiteSavePointPendingSerializer(my_site, data={'wkb_geometry_pending': geom_str}, context={'validate_distance': True})
                                from disturbance.components.proposals.serializers_apiary import ApiarySiteOnProposalProcessedGeometrySaveSerializer
                                serializer = ApiarySiteOnProposalProcessedGeometrySaveSerializer(relation, data={'wkb_geometry_processed': geom_str})
                                serializer.is_valid(raise_exception=True)
                                serializer.save()

                                # Log it
                                self.log_user_action(ProposalUserAction.APIARY_SITE_MOVED.format(apiary_site['id'], prev_coordinates, (apiary_site['coordinates_moved']['lng'], apiary_site['coordinates_moved']['lat'])), request)

                    # Site transfer
                    elif self.application_type.name == ApplicationType.SITE_TRANSFER:
                        for apiary_site in apiary_sites:
                            transfer_site = SiteTransferApiarySite.objects.get(
                                    proposal_apiary=self.proposal_apiary,
                                    apiary_site_on_approval__apiary_site__id=apiary_site.get('id')
                                    )
                            transfer_site.internal_selected = apiary_site.get('checked') if transfer_site.customer_selected else False
                            if apiary_site.get('checked'):
                                apiary_sites_list.append(apiary_site.get('id'))
                            transfer_site.save()

                self.save()
                # Log proposal action
                if self.apiary_group_application_type:
                    self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APIARY_APPROVAL.format(
                        self.id, 
                        self.proposed_issuance_approval.get('start_date'),
                        self.proposed_issuance_approval.get('expiry_date'),
                        #', '.join(apiary_sites_list)
                        str(apiary_sites_list).lstrip('[').rstrip(']')
                        ),request)
                else:
                    self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.id),request)
                # Log entry for organisation
                if self.applicant:
                    if self.apiary_group_application_type:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_APIARY_APPROVAL.format(
                            self.id, 
                            self.proposed_issuance_approval.get('start_date'),
                            self.proposed_issuance_approval.get('expiry_date'),
                            str(apiary_sites_list).lstrip('[').rstrip(']')
                            #', '.join(apiary_sites_list)
                            ),request)
                    else:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.id),request)

                send_approver_approve_email_notification(request, self)
            except:
                raise

    def final_approval_temp_use(self, request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor':
                    # For temporary Use Application, assessor approves it
                    raise ValidationError('You cannot approve the proposal if it is not with an assessor')

                self.proposed_decline_status = False
                self.processing_status = Proposal.PROCESSING_STATUS_APPROVED
                self.customer_status = 'approved'

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)

                # TODO: Email?

                self.save()

            except:
                raise

    def final_decline_temp_use(self, request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor':
                    # For temporary Use Application, assessor approves it
                    raise ValidationError('You cannot approve the proposal if it is not with an assessor')

                # TODO: Is it required to show a modal and get the reason of the delinature or so?  If so, we need following 4 lines
                # proposal_decline, success = ProposalDeclinedDetails.objects.update_or_create(
                #     proposal = self,
                #     defaults={'officer':request.user,'reason':details.get('reason'),'cc_email':details.get('cc_email',None)}
                # )
                self.proposed_decline_status = True
                self.processing_status = 'declined'
                self.customer_status = 'declined'
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id), request)

                # TODO: Email?
                # send_proposal_decline_email_notification(self,request, proposal_decline)

            except:
                raise

    def final_approval(self,request,details):
        from disturbance.components.approvals.models import Approval
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot issue the approval if it is not with an approver')
                #if not self.applicant.organisation.postal_address:
                if not self.relevant_applicant_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal.')

                self.proposed_issuance_approval = {
                    'start_date' : details.get('start_date').strftime('%d/%m/%Y'),
                    'expiry_date' : details.get('expiry_date').strftime('%d/%m/%Y'),
                    'details': details.get('details'),
                    'cc_email':details.get('cc_email')
                }
                self.proposed_decline_status = False
                self.processing_status = 'approved'
                self.customer_status = 'approved'
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)
                #import ipdb;ipdb.set_trace()

                if self.processing_status == 'approved':
                    # TODO if it is an ammendment proposal then check appropriately
                    #import ipdb; ipdb.set_trace()
                    checking_proposal = self
                    if self.proposal_type == 'renewal':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'proxy_applicant' : self.proxy_applicant,
                                    'lodgement_number': previous_approval.lodgement_number,
                                    'apiary_approval': self.apiary_group_application_type,
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()

                    elif self.proposal_type == 'amendment':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'proxy_applicant' : self.proxy_applicant,
                                    'lodgement_number': previous_approval.lodgement_number,
                                    'apiary_approval': self.apiary_group_application_type,
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()
                    else:
                        approval,created = Approval.objects.update_or_create(
                            current_proposal = checking_proposal,
                            defaults = {
                                #'activity' : self.activity,
                                #'region' : self.region.name,
                                #'tenure' : self.tenure.name,
                                #'title' : self.title,
                                'issue_date' : timezone.now(),
                                'expiry_date' : details.get('expiry_date'),
                                'start_date' : details.get('start_date'),
                                'applicant' : self.applicant,
                                'proxy_applicant' : self.proxy_applicant,
                                'apiary_approval': self.apiary_group_application_type,
                                #'extracted_fields' = JSONField(blank=True, null=True)
                            }
                        )
                        #print approval,approval.id, created
                    # Generate compliances
                    #self.generate_compliances(approval, request)
                    from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                    if created:
                        if self.proposal_type == 'amendment':
                            approval_compliances = Compliance.objects.filter(approval= previous_approval, proposal = self.previous_application, processing_status='future')
                            if approval_compliances:
                                for c in approval_compliances:
                                    c.delete()
                        # Log creation
                        # Generate the document
                        approval.generate_doc(request.user)
                        self.generate_compliances(approval, request)
                        # send the doc and log in approval and org
                    else:
                        #approval.replaced_by = request.user
                        #approval.replaced_by = self.approval
                        # Generate the document
                        approval.generate_doc(request.user)
                        #Delete the future compliances if Approval is reissued and generate the compliances again.
                        approval_compliances = Compliance.objects.filter(approval= approval, proposal = self, processing_status='future')
                        if approval_compliances:
                            for c in approval_compliances:
                                c.delete()
                        self.generate_compliances(approval, request)
                        # Log proposal action
                        self.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.id),request)
                        # Log entry for organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.id),request)
                    self.approval = approval
                #send Proposal approval email with attachment
                send_proposal_approval_email_notification(self,request)
                self.save(version_comment='Final Approval: {}'.format(self.approval.lodgement_number))
                self.approval.documents.all().update(can_delete=False)

            except:
                raise



    '''def generate_compliances(self,approval):
        from disturbance.components.compliances.models import Compliance
        today = timezone.now().date()
        timedelta = datetime.timedelta

        for req in self.requirements.all():
            if req.recurrence and req.due_date > today:
                current_date = req.due_date
                while current_date < approval.expiry_date:
                    for x in range(req.recurrence_schedule):
                    #Weekly
                        if req.recurrence_pattern == 1:
                            current_date += timedelta(weeks=1)
                    #Monthly
                        elif req.recurrence_pattern == 2:
                            current_date += timedelta(weeks=4)
                            pass
                    #Yearly
                        elif req.recurrence_pattern == 3:
                            current_date += timedelta(days=365)
                    # Create the compliance
                    if current_date <= approval.expiry_date:
                        Compliance.objects.create(
                            proposal=self,
                            due_date=current_date,
                            processing_status='future',
                            approval=approval,
                            requirement=req.requirement,
                        )
                        #TODO add logging for compliance'''


    def generate_compliances(self,approval, request):
        today = timezone.now().date()
        timedelta = datetime.timedelta
        from disturbance.components.compliances.models import Compliance, ComplianceUserAction
        #For amendment type of Proposal, check for copied requirements from previous proposal
        if self.proposal_type == 'amendment':
            try:
                for r in self.requirements.filter(copied_from__isnull=False):
                    cs=[]
                    cs=Compliance.objects.filter(requirement=r.copied_from, proposal=self.previous_application, processing_status='due')
                    if cs:
                        if r.is_deleted == True:
                            for c in cs:
                                c.processing_status='discarded'
                                c.customer_status = 'discarded'
                                c.reminder_sent=True
                                c.post_reminder_sent=True
                                c.save()
                        if r.is_deleted == False:
                            for c in cs:
                                c.proposal= self
                                c.approval=approval
                                c.requirement=r
                                c.save()
            except:
                raise
        #requirement_set= self.requirements.filter(copied_from__isnull=True).exclude(is_deleted=True)
        requirement_set= self.requirements.all().exclude(is_deleted=True)

        #for req in self.requirements.all():
        for req in requirement_set:
            try:
                if req.due_date and req.due_date >= today:
                    current_date = req.due_date
                    #create a first Compliance
                    try:
                        compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                    except Compliance.DoesNotExist:
                        compliance =Compliance.objects.create(
                                    proposal=self,
                                    due_date=current_date,
                                    processing_status='future',
                                    approval=approval,
                                    requirement=req,
                        )
                        compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
                    if req.recurrence:
                        while current_date < approval.expiry_date:
                            for x in range(req.recurrence_schedule):
                            #Weekly
                                if req.recurrence_pattern == 1:
                                    current_date += timedelta(weeks=1)
                            #Monthly
                                elif req.recurrence_pattern == 2:
                                    current_date += timedelta(weeks=4)
                                    pass
                            #Yearly
                                elif req.recurrence_pattern == 3:
                                    current_date += timedelta(days=365)
                            # Create the compliance
                            if current_date <= approval.expiry_date:
                                try:
                                    compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                                except Compliance.DoesNotExist:
                                    compliance =Compliance.objects.create(
                                                proposal=self,
                                                due_date=current_date,
                                                processing_status='future',
                                                approval=approval,
                                                requirement=req,
                                    )
                                    compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
            except:
                raise

    def renew_approval(self,request):
        #import ipdb; ipdb.set_trace()
        with transaction.atomic():
            previous_proposal = self
            try:
                proposal=Proposal.objects.get(previous_application = previous_proposal)
                if proposal.customer_status=='with_assessor':
                    raise ValidationError('A renewal for this licence has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                if previous_proposal.apiary_group_application_type:
                    proposal = clone_apiary_proposal_with_status_reset(previous_proposal)
                else:
                    previous_proposal = Proposal.objects.get(id=self.id)
                    proposal = clone_proposal_with_status_reset(previous_proposal)
                    #proposal.schema = ProposalType.objects.first().schema
                    ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                    proposal.schema = ptype.schema
                proposal.proposal_type = 'renewal'
                proposal.submitter = request.user
                proposal.previous_application = self
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.id),request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.id),request)
                #Log entry for approval
                from disturbance.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_RENEW_APPROVAL.format(self.approval.id),request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal

    def amend_approval(self,request):
        with transaction.atomic():
            previous_proposal = self
            try:
                amend_conditions = {
                'previous_application': previous_proposal,
                'proposal_type': 'amendment'

                }
                proposal=Proposal.objects.get(**amend_conditions)
                if proposal.customer_status=='under_review':
                    raise ValidationError('An amendment for this licence has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                previous_proposal = Proposal.objects.get(id=self.id)
                proposal = clone_proposal_with_status_reset(previous_proposal)
                proposal.proposal_type = 'amendment'
                #proposal.schema = ProposalType.objects.first().schema
                ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                proposal.schema = ptype.schema
                proposal.submitter = request.user
                proposal.previous_application = self
                #copy all the requirements from the previous proposal
                #req=self.requirements.all()
                req=self.requirements.all().exclude(is_deleted=True)
                from copy import deepcopy
                if req:
                    for r in req:
                        old_r = deepcopy(r)
                        r.proposal = proposal
                        r.copied_from=old_r
                        r.id = None
                        r.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.id),request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.id),request)
                #Log entry for approval
                from disturbance.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_AMEND_APPROVAL.format(self.approval.id),request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal

    def internal_view_log(self,request):
        self.log_user_action(ProposalUserAction.ACTION_VIEW_PROPOSAL.format(self.id),request)
        return self
    
    def apiary_requirements(self, approval=None):
        if self.application_type.name == ApplicationType.SITE_TRANSFER and approval:
            return self.requirements.filter(apiary_approval=approval)
        else:
            return self.requirements.all()


class ProposalLogDocument(Document):
    log_entry = models.ForeignKey('ProposalLogEntry',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_comms_log_filename)

    class Meta:
        app_label = 'disturbance'

class ProposalLogEntry(CommunicationsLogEntry):
    proposal = models.ForeignKey(Proposal, related_name='comms_logs')

    class Meta:
        app_label = 'disturbance'

    def save(self, **kwargs):
        # save the application reference if the reference not provided
        if not self.reference:
            self.reference = self.proposal.reference
        super(ProposalLogEntry, self).save(**kwargs)

class AmendmentRequestDocument(Document):
    amendment_request = models.ForeignKey('AmendmentRequest',related_name='amendment_request_documents')
    _file = models.FileField(upload_to=update_amendment_request_doc_filename, max_length=500)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(AmendmentRequestDocument, self).delete()

class ProposalRequest(models.Model):
    proposal = models.ForeignKey(Proposal)
    subject = models.CharField(max_length=200, blank=True)
    text = models.TextField(blank=True)
    officer = models.ForeignKey(EmailUser, null=True)

    class Meta:
        app_label = 'disturbance'

class ComplianceRequest(ProposalRequest):
    REASON_CHOICES = (('outstanding', 'There are currently outstanding returns for the previous licence'),
                      ('other', 'Other'))
    reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])

    class Meta:
        app_label = 'disturbance'


class AmendmentReason(models.Model):
    reason = models.CharField('Reason', max_length=125)

    class Meta:
        app_label = 'disturbance'
        verbose_name = "Proposal Amendment Reason" # display name in Admin
        verbose_name_plural = "Proposal Amendment Reasons"

    def __str__(self):
        return self.reason



class AmendmentRequest(ProposalRequest):
    STATUS_CHOICES = (('requested', 'Requested'), ('amended', 'Amended'))
    #REASON_CHOICES = (('insufficient_detail', 'The information provided was insufficient'),
    #                  ('missing_information', 'There was missing information'),
    #                  ('other', 'Other'))
    # try:
    #     # model requires some choices if AmendmentReason does not yet exist or is empty
    #     REASON_CHOICES = list(AmendmentReason.objects.values_list('id', 'reason'))
    #     if not REASON_CHOICES:
    #         REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                           (1, 'There was missing information'),
    #                           (2, 'Other'))
    # except:
    #     REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                       (1, 'There was missing information'),
    #                       (2, 'Other'))


    status = models.CharField('Status', max_length=30, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    #reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])
    reason = models.ForeignKey(AmendmentReason, blank=True, null=True)
    #reason = models.ForeignKey(AmendmentReason)

    class Meta:
        app_label = 'disturbance'

    def generate_amendment(self,request):
        with transaction.atomic():
            try:
                if not self.proposal.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.status == 'requested':
                    proposal = self.proposal
                    if proposal.processing_status != 'draft':
                        proposal.processing_status = 'draft'
                        proposal.customer_status = 'draft'
                        proposal.save()
                        proposal.documents.all().update(can_hide=True)

                    # Create a log entry for the proposal
                    proposal.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS, request)
                    # Create a log entry for the organisation
                    if proposal.applicant:
                        proposal.applicant.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS, request)

                    # send email

                    send_amendment_email_notification(self,request, proposal)

                self.save()
            except:
                raise

    def add_documents(self, request):
        with transaction.atomic():
            try:
                # save the files
                data = json.loads(request.data.get('data'))
                if not data.get('update'):
                    documents_qs = self.amendment_request_documents.filter(input_name='amendment_request_doc', visible=True)
                    documents_qs.delete()
                for idx in range(data['num_files']):
                    _file = request.data.get('file-'+str(idx))
                    document = self.amendment_request_documents.create(_file=_file, name=_file.name)
                    document.input_name = data['input_name']
                    document.can_delete = True
                    document.save()
                # end save documents
                self.save()
            except:
                raise
        return

class Assessment(ProposalRequest):
    STATUS_CHOICES = (('awaiting_assessment', 'Awaiting Assessment'), ('assessed', 'Assessed'),
                      ('assessment_expired', 'Assessment Period Expired'))
    assigned_assessor = models.ForeignKey(EmailUser, blank=True, null=True)
    status = models.CharField('Status', max_length=20, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    date_last_reminded = models.DateField(null=True, blank=True)
    #requirements = models.ManyToManyField('Requirement', through='AssessmentRequirement')
    comment = models.TextField(blank=True)
    purpose = models.TextField(blank=True)

    class Meta:
        app_label = 'disturbance'

class ProposalDeclinedDetails(models.Model):
    proposal = models.OneToOneField(Proposal)
    officer = models.ForeignKey(EmailUser, null=False)
    reason = models.TextField(blank=True)
    cc_email = models.TextField(null=True)

    class Meta:
        app_label = 'disturbance'

@python_2_unicode_compatible
#class ProposalStandardRequirement(models.Model):
class ProposalStandardRequirement(RevisionedMixin):
    SYSTEM_CHOICES = (
            ('disturbance', 'Disturbance'), 
            ('apiary', 'Apiary'),
                      )
    system = models.CharField('System', max_length=20, choices=SYSTEM_CHOICES, default=SYSTEM_CHOICES[0][0])
    text = models.TextField()
    code = models.CharField(max_length=10, unique=True)
    obsolete = models.BooleanField(default=False)

    def __str__(self):
        return self.code

    class Meta:
        app_label = 'disturbance'


#class ReferralRecipientGroup(models.Model):
class ApiaryReferralGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        #return 'Referral Recipient Group'
        return self.name

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    @property
    def members_list(self):
            return list(self.members.all().values_list('email', flat=True))

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]


    class Meta:
        app_label = 'disturbance'
        verbose_name = "Apiary Referral Group"
        verbose_name_plural = "Apiary Referral groups"


class ProposalRequirement(OrderedModel):
    RECURRENCE_PATTERNS = [(1, 'Weekly'), (2, 'Monthly'), (3, 'Yearly')]
    standard_requirement = models.ForeignKey(ProposalStandardRequirement,null=True,blank=True)
    free_requirement = models.TextField(null=True,blank=True)
    standard = models.BooleanField(default=True)
    proposal = models.ForeignKey(Proposal,related_name='requirements')
    due_date = models.DateField(null=True,blank=True)
    recurrence = models.BooleanField(default=False)
    recurrence_pattern = models.SmallIntegerField(choices=RECURRENCE_PATTERNS,default=1)
    recurrence_schedule = models.IntegerField(null=True,blank=True)
    copied_from = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    #site_transfer_approval = models.ForeignKey('disturbance.Approval',null=True,blank=True)
    apiary_approval = models.ForeignKey('disturbance.Approval',null=True,blank=True)
    #order = models.IntegerField(default=1)
    # referral_group is no longer required for Apiary
    referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='apiary_requirement_referral_groups')

    class Meta:
        app_label = 'disturbance'


    @property
    def requirement(self):
        return self.standard_requirement.text if self.standard else self.free_requirement

# no longer required for Apiary
#class RequirementDocument(Document):
#    #requirement = models.ForeignKey('ProposalRequirement',related_name='requirement_documents')
#    requirement = models.ForeignKey('ProposalRequirement',related_name='documents')
#    #_file = models.FileField(upload_to=update_requirement_doc_filename, max_length=512)
#    _file = models.FileField(max_length=512)
#    input_name = models.CharField(max_length=255,null=True,blank=True)
#    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
#    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history
#
#    def delete(self):
#        if self.can_delete:
#            return super(RequirementDocument, self).delete()
#
#    class Meta:
#        app_label = 'disturbance'


class ProposalUserAction(UserAction):
    ACTION_CREATE_CUSTOMER_ = "Create customer {}"
    ACTION_CREATE_PROFILE_ = "Create profile {}"
    ACTION_LODGE_APPLICATION = "Lodge proposal {}"
    ACTION_SAVE_APPLICATION = "Save proposal {}"
    ACTION_ASSIGN_TO_ASSESSOR = "Assign proposal {} to {} as the assessor"
    ACTION_UNASSIGN_ASSESSOR = "Unassign assessor from proposal {}"
    ACTION_ASSIGN_TO_APPROVER = "Assign proposal {} to {} as the approver"
    ACTION_UNASSIGN_APPROVER = "Unassign approver from proposal {}"
    ACTION_ACCEPT_ID = "Accept ID"
    ACTION_RESET_ID = "Reset ID"
    ACTION_ID_REQUEST_UPDATE = 'Request ID update'
    ACTION_ACCEPT_CHARACTER = 'Accept character'
    ACTION_RESET_CHARACTER = "Reset character"
    ACTION_ACCEPT_REVIEW = 'Accept review'
    ACTION_RESET_REVIEW = "Reset review"
    ACTION_ID_REQUEST_AMENDMENTS = "Request amendments"
    ACTION_SEND_FOR_ASSESSMENT_TO_ = "Send for assessment to {}"
    ACTION_SEND_ASSESSMENT_REMINDER_TO_ = "Send assessment reminder to {}"
    ACTION_DECLINE = "Decline proposal {}"
    ACTION_ENTER_CONDITIONS = "Enter requirement"
    ACTION_CREATE_CONDITION_ = "Create requirement {}"
    ACTION_ISSUE_APPROVAL_ = "Issue Approval for proposal {}"
    ACTION_UPDATE_APPROVAL_ = "Update Approval for proposal {}"
    ACTION_EXPIRED_APPROVAL_ = "Expire Approval for proposal {}"
    ACTION_DISCARD_PROPOSAL = "Discard proposal {}"
    ACTION_APPROVAL_LEVEL_DOCUMENT = "Assign Approval level document {}"
    ACTION_APPROVAL_LEVEL_COMMENT = "Save Approval level comment {}"
    ACTION_VIEW_PROPOSAL = "View Proposal {}"
    # Assessors
    ACTION_SAVE_ASSESSMENT_ = "Save assessment {}"
    ACTION_CONCLUDE_ASSESSMENT_ = "Conclude assessment {}"
    ACTION_PROPOSED_APPROVAL = "Proposal {} has been proposed for approval"
    ACTION_PROPOSED_APIARY_APPROVAL = "Proposal {} has been proposed for approval with start date {}, expiry date {} and apiary sites {}"
    ACTION_PROPOSED_DECLINE = "Proposal {} has been proposed for decline"
    # Referrals
    ACTION_SEND_REFERRAL_TO = "Send referral {} for proposal {} to {}"
    ACTION_RESEND_REFERRAL_TO = "Resend referral {} for proposal {} to {}"
    ACTION_REMIND_REFERRAL = "Send reminder for referral {} for proposal {} to {}"
    ACTION_ENTER_REQUIREMENTS = "Enter Requirements for proposal {}"
    ACTION_BACK_TO_PROCESSING = "Back to processing for proposal {}"
    RECALL_REFERRAL = "Referral {} for proposal {} has been recalled"
    CONCLUDE_REFERRAL = "Referral {} for proposal {} has been concluded by {}"
    #Approval
    ACTION_REISSUE_APPROVAL = "Reissue approval for proposal {}"
    ACTION_CANCEL_APPROVAL = "Cancel approval for proposal {}"
    ACTION_SUSPEND_APPROVAL = "Suspend approval for proposal {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate approval for proposal {}"
    ACTION_SURRENDER_APPROVAL = "Surrender approval for proposal {}"
    ACTION_RENEW_PROPOSAL = "Create Renewal proposal for proposal {}"
    ACTION_AMEND_PROPOSAL = "Create Amendment proposal for proposal {}"
    # Apiary Actions
    APIARY_ACTION_SEND_REFERRAL_TO = "Send Apiary referral {} for application {} to {}"
    APIARY_ACTION_RESEND_REFERRAL_TO = "Resend Apiary referral {} for application {} to {}"
    APIARY_ACTION_REMIND_REFERRAL = "Send reminder for Apiary referral {} for application {} to {}"
    APIARY_ACTION_ENTER_REQUIREMENTS = "Enter Requirements for application {}"
    APIARY_ACTION_BACK_TO_PROCESSING = "Back to processing for application {}"
    APIARY_RECALL_REFERRAL = "Apiary Referral {} for application {} has been recalled"
    APIARY_CONCLUDE_REFERRAL = "Apiary Referral {} for application {} has been concluded by {}"
    APIARY_ACTION_SAVE_APPLICATION = "Save Apiary application {}"
    APIARY_SITE_MOVED = "Apiary Site {} has been moved from {} to {}"



    class Meta:
        app_label = 'disturbance'
        ordering = ('-when',)

    @classmethod
    def log_action(cls, proposal, action, user):
        return cls.objects.create(
            proposal=proposal,
            who=user,
            what=str(action)
        )

    proposal = models.ForeignKey(Proposal, related_name='action_logs')



class Referral(models.Model):
    SENT_CHOICES = (
        (1,'Sent From Assessor'),
        (2,'Sent From Referral')
    )
    PROCESSING_STATUS_CHOICES = (
                                 ('with_referral', 'Awaiting'),
                                 ('recalled', 'Recalled'),
                                 ('completed', 'Completed'),
                                 )
    lodged_on = models.DateTimeField(auto_now_add=True)
    proposal = models.ForeignKey(Proposal,related_name='referrals')
    sent_by = models.ForeignKey(EmailUser,related_name='disturbance_assessor_referrals')
    referral = models.ForeignKey(EmailUser,null=True,blank=True,related_name='disturbance_referalls')
    linked = models.BooleanField(default=False)
    sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[0][0])
    text = models.TextField(blank=True) #Assessor text
    referral_text = models.TextField(blank=True)


    class Meta:
        app_label = 'disturbance'
        ordering = ('-lodged_on',)

    def __str__(self):
        return 'Proposal {} - Referral {}'.format(self.proposal.id,self.id)

    # Methods
    @property
    def latest_referrals(self):
        return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[:2]

    @property
    def can_be_completed(self):
        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
        qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
        if qs:
            return False
        else:
            return True

    def recall(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'recalled'
            self.save()
            send_referral_recall_email_notification(self, request)
            # TODO Log proposal action
            self.proposal.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)
            # TODO log organisation action
            self.proposal.applicant.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)

    def remind(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            # Create a log entry for the proposal
            self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # Create a log entry for the organisation
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # send email
            send_referral_email_notification(self,request,reminder=True)

    def resend(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'with_referral'
            self.proposal.processing_status = 'with_referral'
            self.proposal.save()
            self.sent_from = 1
            self.save()
            # Create a log entry for the proposal
            self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # Create a log entry for the organisation
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # send email
            send_referral_email_notification(self,request)

    def complete(self,request, referral_comment):
        with transaction.atomic():
            try:
                if request.user != self.referral:
                    raise exceptions.ReferralNotAuthorized()
                self.processing_status = 'completed'
                self.referral_text = referral_comment
                self.save()
                # TODO Log proposal action
                self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                # TODO log organisation action
                self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                send_referral_complete_email_notification(self,request)
            except:
                raise

    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                referral_email = referral_email.lower()
                if self.proposal.processing_status == 'with_referral':
                    if request.user != self.referral:
                        raise exceptions.ReferralNotAuthorized()
                    if self.sent_from != 1:
                        raise exceptions.ReferralCanNotSend()
                    self.proposal.processing_status = 'with_referral'
                    self.proposal.save()
                    referral = None
                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email)
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    qs=Referral.objects.filter(sent_by=user, proposal=self.proposal)
                    if qs:
                        raise ValidationError('You cannot send referral to this user')
                    try:
                        Referral.objects.get(referral=user,proposal=self.proposal)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self.proposal,
                            referral=user,
                            sent_by=request.user,
                            sent_from=2,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    self.proposal.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # Create a log entry for the organisation
                    self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # send email
                    send_referral_email_notification(referral,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    # Properties
    @property
    def region(self):
        return self.proposal.region

    @property
    def activity(self):
        return self.proposal.activity

    @property
    def title(self):
        return self.proposal.title

    @property
    def applicant(self):
        return self.proposal.applicant.name

    @property
    def can_be_processed(self):
        return self.processing_status == 'with_referral'

    def can_assess_referral(self,user):
        return self.processing_status == 'with_referral'

@receiver(pre_delete, sender=Proposal)
def delete_documents(sender, instance, *args, **kwargs):
    for document in instance.documents.all():
        document.delete()

def clone_proposal_with_status_reset(proposal):
        with transaction.atomic():
            try:
                proposal.customer_status = 'draft'
                proposal.processing_status = 'draft'
                proposal.assessor_data = None
                proposal.comment_data = None

                #proposal.id_check_status = 'not_checked'
                #proposal.character_check_status = 'not_checked'
                #proposal.compliance_check_status = 'not_checked'
                #Sproposal.review_status = 'not_reviewed'

                proposal.lodgement_number = ''
                proposal.lodgement_sequence = 0
                proposal.lodgement_date = None

                proposal.assigned_officer = None
                proposal.assigned_approver = None

                proposal.approval = None

                original_proposal_id = proposal.id

                #proposal.previous_application = Proposal.objects.get(id=original_proposal_id)

                proposal.id = None
                proposal.approval_level_document = None

                proposal.save(no_revision=True)

                # clone documents
                for proposal_document in ProposalDocument.objects.filter(proposal=original_proposal_id):
                    proposal_document.proposal = proposal
                    proposal_document.id = None
                    proposal_document._file.name = u'proposals/{}/documents/{}'.format(proposal.id, proposal_document.name)
                    proposal_document.can_delete = True
                    proposal_document.save()

                # copy documents on file system and reset can_delete flag
                subprocess.call('cp -pr media/proposals/{} media/proposals/{}'.format(original_proposal_id, proposal.id), shell=True)

                return proposal
            except:
                raise


def clone_apiary_proposal_with_status_reset(original_proposal):
    #import ipdb; ipdb.set_trace()
    with transaction.atomic():
        try:
            proposal = copy.deepcopy(original_proposal)
            proposal.id = None
            #proposal.application_type = ApplicationType.objects.filter(name=ApplicationType.APIARY)[0]
            proposal.application_type = ApplicationType.objects.get(name=ApplicationType.APIARY)

            proposal.save(no_revision=True)
            # create proposal_apiary and associate it with the proposal
            proposal_apiary = ProposalApiary.objects.create(proposal=proposal)
            #proposal_apiary = proposal_apiary.proposal
            proposal_apiary.save()

            proposal.customer_status = 'draft'
            proposal.processing_status = 'draft'
            proposal.assessor_data = None
            proposal.comment_data = None
            proposal.lodgement_number = ''
            proposal.lodgement_sequence = 0
            proposal.lodgement_date = None

            proposal.assigned_officer = None
            proposal.assigned_approver = None

            #proposal.approval = None

            #original_proposal_id = proposal.id

            #proposal.id = None
            proposal.approval_level_document = None
            proposal.fee_invoice_reference = None
            proposal.activity = 'Apiary Renewal'

            proposal.save(no_revision=True)

            ## clone documents
            #for proposal_document in DeedPollDocument.objects.filter(proposal=original_proposal.id):

            #    proposal_document.proposal = proposal
            #    proposal_document.id = None
            #    path = default_storage.save(
            #        '{}/proposals/{}/deed_poll_documents/{}'.format(
            #            settings.MEDIA_APIARY_DIR, proposal.id, proposal_document.name), ContentFile(
            #            proposal_document._file.read()))

            #    proposal_document._file = path
            #    proposal_document.can_delete = True
            #    proposal_document.save()

            # copy documents on file system and reset can_delete flag
            #subprocess.call('cp -pr media/proposals/{} media/proposals/{}'.format(original_proposal.id, proposal.id), shell=True)

            # clone requirements
            approval = original_proposal.proposal_apiary.retrieve_approval
            req = approval.proposalrequirement_set.exclude(is_deleted=True)
            #from copy import deepcopy
            if req:
                for r in req:
                    old_r = copy.deepcopy(r)
                    r.proposal = proposal
                    r.copied_from=old_r
                    r.id = None
                    r.save()

            # update apiary_sites with new proposal
            approval.add_apiary_sites_to_proposal_apiary_for_renewal(proposal_apiary)
            # for site in approval.apiary_sites.all():
                # Create new relations between the ApiarySite and the ProposalApiary
                # ApiarySiteOnProposal.objects.create(apiary_site=site, proposal_apiary=proposal.proposal_apiary)
                # site.proposal_apiary = proposal.proposal_apiary
                # site.save()

            # Checklist questions
            for question in ApiaryChecklistQuestion.objects.filter(
                    checklist_type='apiary',
                    checklist_role='applicant'
                    ):
                new_answer = ApiaryChecklistAnswer.objects.create(proposal = proposal.proposal_apiary,
                                                                           question = question)

            # update approval.current_proposal
            #approval.current_proposal = proposal
            #approval.save()
            # Set previous_application to maintain proposal history
            #proposal_apiary.proposal.previous_application = approval.current_proposal
            #proposal_apiary.proposal.save()

            return proposal
        except:
            raise

def searchKeyWords(searchWords, searchProposal, searchApproval, searchCompliance, is_internal= True):
    from disturbance.utils import search, search_approval, search_compliance
    from disturbance.components.approvals.models import Approval
    from disturbance.components.compliances.models import Compliance
    qs = []
    if is_internal:
        proposal_list = Proposal.objects.filter(application_type__name='Disturbance').exclude(processing_status__in=['discarded','draft'])
        approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
        compliance_list = Compliance.objects.all()
    if searchWords:
        if searchProposal:
            for p in proposal_list:
                if p.data:
                    try:
                        results = search(p.data[0], searchWords)
                        final_results = {}
                        if results:
                            for r in results:
                                for key, value in r.iteritems():
                                    final_results.update({'key': key, 'value': value})
                            res = {
                                'number': p.lodgement_number,
                                'id': p.id,
                                'type': 'Proposal',
                                'applicant': p.applicant.name,
                                'text': final_results,
                                }
                            qs.append(res)
                    except:
                        raise
        if searchApproval:
            for a in approval_list:
                try:
                    results = search_approval(a, searchWords)
                    qs.extend(results)
                except:
                    raise
        if searchCompliance:
            for c in compliance_list:
                try:
                    results = search_compliance(c, searchWords)
                    qs.extend(results)
                except:
                    raise
    return qs

def search_reference(reference_number):
    from disturbance.components.approvals.models import Approval
    from disturbance.components.compliances.models import Compliance
    proposal_list = Proposal.objects.all().exclude(processing_status__in=['discarded'])
    approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
    compliance_list = Compliance.objects.all().exclude(processing_status__in=['future'])
    record = {}
    try:
        result = proposal_list.get(lodgement_number = reference_number)
        record = {  'id': result.id,
                    'type': 'proposal' }
    except Proposal.DoesNotExist:
        try:
            result = approval_list.get(lodgement_number = reference_number)
            record = {  'id': result.id,
                        'type': 'approval' }
        except Approval.DoesNotExist:
            try:
                for c in compliance_list:
                    if c.reference == reference_number:
                        record = {  'id': c.id,
                                    'type': 'compliance' }
            except:
                raise ValidationError('Record with provided reference number does not exist')
    if record:
        return record
    else:
        raise ValidationError('Record with provided reference number does not exist')


from ckeditor.fields import RichTextField
class HelpPage(models.Model):
    HELP_TEXT_EXTERNAL = 1
    HELP_TEXT_INTERNAL = 2
    HELP_TYPE_CHOICES = (
        (HELP_TEXT_EXTERNAL, 'External'),
        (HELP_TEXT_INTERNAL, 'Internal'),
    )

    application_type = models.ForeignKey(ApplicationType)
    content = RichTextField()
    description = models.CharField(max_length=256, blank=True, null=True)
    help_type = models.SmallIntegerField('Help Type', choices=HELP_TYPE_CHOICES, default=HELP_TEXT_EXTERNAL)
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    class Meta:
        app_label = 'disturbance'
        unique_together = ('application_type', 'help_type', 'version')


# --------------------------------------------------------------------------------------
# Apiary Models Start
# --------------------------------------------------------------------------------------
class ApiarySiteOnProposal(RevisionedMixin):
    apiary_site = models.ForeignKey('ApiarySite',)
    proposal_apiary = models.ForeignKey('ProposalApiary',)
    apiary_site_status_when_submitted = models.CharField(max_length=40, blank=True)
    for_renewal = models.BooleanField(default=False)
    site_status = models.CharField(default=SITE_STATUS_DRAFT, max_length=20)
    making_payment = models.BooleanField(default=False)
    workflow_selected_status = models.BooleanField(default=False)  # This field is used only during approval process to select/deselect the site to be approved
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    wkb_geometry_draft = PointField(srid=4326, blank=True, null=True)  # store the coordinates before submit
    wkb_geometry_processed = PointField(srid=4326, blank=True, null=True)  # store approved coordinates
    site_category_draft = models.ForeignKey('SiteCategory', null=True, blank=True, related_name='intermediate_draft')
    site_category_processed = models.ForeignKey('SiteCategory', null=True, blank=True, related_name='intermediate_processed')
    objects = GeoManager()

    def __str__(self):
        return 'id:{}: (apiary_site: {}, proposal_apiary: {})'.format(self.id, self.apiary_site.id, self.proposal_apiary.id)

    def apiary_site_not_under_payment(self):
        value = True
        if self.apiary_site.is_vacant:
            qs = ApiarySiteOnProposal.objects

        return value

    class Meta:
        app_label = 'disturbance'
        unique_together = ['apiary_site', 'proposal_apiary',]


class ProposalApiary(RevisionedMixin):
    title = models.CharField('Title', max_length=200, null=True)
    location = gis_models.PointField(srid=4326, blank=True, null=True)
    proposal = models.OneToOneField(Proposal, related_name='proposal_apiary', null=True)

    # We don't use GIS field, because these are just fields user input into the <input> field
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # required for Site Transfer applications
    transferee = models.ForeignKey(EmailUser, blank=True, null=True, related_name='apiary_transferee')
    originating_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name="site_transfer_originating_approval")
    target_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name="site_transfer_target_approval")

    apiary_sites = models.ManyToManyField('ApiarySite', through=ApiarySiteOnProposal, related_name='proposal_apiary_set')

    def __str__(self):
        return 'id:{} - {}'.format(self.id, self.title)

    class Meta:
        app_label = 'disturbance'

    def validate_apiary_sites(self, raise_exception=False):
        validity = True

        # Check if the site has been already taken by someone else
        for apiary_site in self.apiary_sites.all():
            if apiary_site.is_vacant:
                # The site is 'vacant'
                others = ApiarySiteOnProposal.objects.filter(apiary_site=apiary_site, making_payment=True).exclude(proposal_apiary=self)
                if others:
                    # Someone has been making payment for this apiary site
                    validity = False
            else:
                # The site is not 'vacant'
                relation = self.get_relation(apiary_site)
                if relation != apiary_site.latest_proposal_link:
                    # This site was 'vacant' site when selected, but it's already taken by someone else
                    validity = False

            if not validity and raise_exception:
                # raise ValidationError(message='The vacant apiary site: {} is no longer available.'.format(apiary_site.id), params={'apiary_site_id': apiary_site.id})
                raise serializers.ValidationError({
                    'type': 'site_no_longer_available',
                    'message': 'The vacant apiary site: {} is no longer available.'.format(apiary_site.id),
                    'apiary_site_id': apiary_site.id})

        # Check the distance between the requested sites
        for apiary_site in self.apiary_sites.all():
            relation = self.get_relation(apiary_site)
            # Check among the apiary sites in this proposal except current one of the loop
            q_objects = Q(apiary_site__in=self.apiary_sites.all())
            q_objects &= Q(wkb_geometry_draft__distance_lte=(relation.wkb_geometry_draft, Distance(m=RESTRICTED_RADIUS)))
            qs_sites_within = ApiarySiteOnProposal.objects.filter(q_objects).exclude(apiary_site=apiary_site)
            if qs_sites_within:
                # In this proposal, there are apiary sites which are too close to each other
                if raise_exception:
                    # raise serializers.ValidationError(['There are apiary sites in this proposal which are too close to each other.',])
                    raise ValidationError('There are apiary sites in this proposal which are too close to each other.')
                validity = False

        return validity

    def final_decline(self):
        relations = self.get_relations()
        relations.update(site_status=SITE_STATUS_DENIED)

    def post_payment_success(self):
        """
        Run this function just after the payment success
        """
        for relation in self.get_relations():
            if relation.apiary_site.is_vacant:
                relation.apiary_site.is_vacant = False
            relation.apiary_site_status_when_submitted = relation.site_status
            relation.wkb_geometry_processed = relation.wkb_geometry_draft
            relation.site_category_processed = relation.site_category_draft
            relation.site_status = SITE_STATUS_PENDING
            relation.making_payment = False  # This should replace the above line
            relation.save()

    def set_workflow_selected_status(self, apiary_site, selected_status):
        relation_obj = self.get_relation(apiary_site)
        relation_obj.workflow_selected_status = selected_status
        relation_obj.save()

    def get_wkb_geometry_processed(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.wkb_geometry_processed

    def get_wkb_geometry_draft(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.wkb_geometry_draft

    def get_workflow_selected_status(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.workflow_selected_status

    def get_status(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.site_status

    def set_status(self, apiary_site, status):
        relation_obj = self.get_relation(apiary_site)
        relation_obj.site_status = status
        relation_obj.save()

    def get_relation(self, apiary_site):
        if isinstance(apiary_site, dict):
            apiary_site = ApiarySite.objects.get(id=apiary_site['id'])
        relation_obj = ApiarySiteOnProposal.objects.get(apiary_site=apiary_site, proposal_apiary=self)
        return relation_obj

    def get_relations(self):
        relation_objs = ApiarySiteOnProposal.objects.filter(apiary_site__in=self.apiary_sites.all(), proposal_apiary=self)
        return relation_objs

    def delete_relation(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        site_status_to_remove = relation_obj.site_status

        # Remove the relationship to the apiary_site
        relation_obj.delete()

        # Delete the apiary site itself if the status of it is 'draft'
        if site_status_to_remove == SITE_STATUS_DRAFT:
            if apiary_site.is_vacant:
                # 'vacant' site should not be deleted, the process should not reach here though
                pass
            else:
                # When removing the relation to the draft site, we don't need both the relation to the site and the site itself
                apiary_site.delete()

    def send_referral(self, request, group_id, referral_text):
        with transaction.atomic():
            try:
                if self.proposal.processing_status == 'with_assessor' or self.proposal.processing_status == 'with_referral':
                    self.proposal.processing_status = 'with_referral'
                    self.proposal.save()
                    self.save()
                    referral = None
                    #import ipdb; ipdb.set_trace()

                    # Check if the user is in ledger
                    try:
                        #referral_group = ApiaryReferralGroup.objects.get(name__icontains=referral_email)
                        referral_group = ApiaryReferralGroup.objects.get(id=group_id)
                    except ApiaryReferralGroup.DoesNotExist:
                        raise exceptions.ProposalReferralCannotBeSent()
                    #try:
                    existing_referrals = Referral.objects.filter(proposal=self.proposal)
                    #if existing_referral:
                    apiary_referral_list = ApiaryReferral.objects.filter(referral_group=referral_group,referral__in=existing_referrals) if existing_referrals else None
                    if apiary_referral_list:
                        raise ValidationError('A referral has already been sent to this group')
                    #except Referral.DoesNotExist:
                    # Create referral if it does not exist for referral_group
                    else:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self.proposal,
                            #referral=user,
                            #referral_group=referral_group,
                            sent_by=request.user,
                            text=referral_text
                        )
                        # Create corresponding ApiaryReferral
                        apiary_referral = ApiaryReferral.objects.create(
                            #proposal = self.referral.proposal,
                            referral=referral,
                            referral_group=referral_group,
                            #sent_by=request.user,
                            #text=referral_text
                        )
                        # create referral checklist answers
                        for question in ApiaryChecklistQuestion.objects.filter(
                                checklist_type='apiary',
                                checklist_role='referrer'
                                ):
                            new_answer = ApiaryChecklistAnswer.objects.create(
                                    proposal = self,
                                    apiary_referral = apiary_referral,
                                    question = question
                                    )

                        # Create a log entry for the proposal
                        #self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                        self.proposal.log_user_action(
                                ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                    referral.id,
                                    self.proposal.id,
                                    '{}'.format(referral_group.name)
                                    ),
                                request
                                )
                        # Create a log entry for the organisation
                        #self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                        applicant_field=getattr(self.proposal, self.proposal.applicant_field)
                        applicant_field.log_user_action(
                                ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                    referral.id,
                                    self.proposal.id,
                                    '{}'.format(referral_group.name)),
                                request
                                )
                        # send email
                        recipients = referral_group.members_list
                        send_apiary_referral_email_notification(referral, recipients, request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    @property
    def customer_status(self):
        return self.proposal.customer_status

    @property
    def processing_status(self):
        return self.proposal.processing_status

    @property
    def retrieve_approval(self):
        from disturbance.components.approvals.models import Approval
        approval = None
        if self.proposal.applicant:
            approval = Approval.objects.filter(applicant=self.proposal.applicant, status=Approval.STATUS_CURRENT, apiary_approval=True).first()
        elif self.proposal.proxy_applicant:
            approval = Approval.objects.filter(proxy_applicant=self.proposal.proxy_applicant, status=Approval.STATUS_CURRENT, apiary_approval=True).first()
        return approval

    # ProposalApiary final approval
    def final_approval(self,request,details):
        #import ipdb;ipdb.set_trace()
        from disturbance.components.approvals.models import Approval
        with transaction.atomic():
            try:
                approval = None
                if self.proposal.application_type.name == 'Apiary':
                #if self.proposal.application_type.name == ApplicationType.APIARY:
                    approval = self.retrieve_approval
                elif self.proposal.application_type.name == 'Site Transfer':
                #elif self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                    #approval = self.proposal.approval
                    target_approval = self.target_approval
                    originating_approval = self.originating_approval

                #approval = None
                #approval = self.retrieve_approval()
                created = None
                if not self.proposal.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.proposal.processing_status != 'with_approver':
                    raise ValidationError('You cannot issue the approval if it is not with an approver')
                #if not self.applicant.organisation.postal_address:
                if not self.proposal.relevant_applicant_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal.')
                start_date = details.get('start_date').strftime('%d/%m/%Y') if details.get('start_date') else None
                expiry_date = details.get('expiry_date').strftime('%d/%m/%Y') if details.get('expiry_date') else None
                self.proposal.proposed_issuance_approval = {
                        'start_date' : start_date,
                        'expiry_date' : expiry_date,
                        'details' : details.get('details'),
                        'cc_email' : details.get('cc_email'),
                }
                self.save()

                self.proposal.proposed_decline_status = False
                self.proposal.processing_status = 'approved'
                self.proposal.customer_status = 'approved'
                # Log proposal action
                self.proposal.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.proposal.id), request)
                # Log entry for organisation
                if self.proposal.applicant:
                    self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.proposal.id), request)
                #import ipdb;ipdb.set_trace()

                if self.proposal.processing_status == 'approved':
                    #import ipdb; ipdb.set_trace()
                    checking_proposal = self.proposal
                    if self.proposal.proposal_type == 'renewal':
                        # TODO - fix for apiary approval
                        pass
                    #    if self.proposal.previous_application:
                    #        previous_approval = self.proposal.previous_application.approval
                    #        approval,created = Approval.objects.update_or_create(
                    #            current_proposal = checking_proposal,
                    #            defaults = {
                    #                #'activity' : self.activity,
                    #                #'region' : self.region,
                    #                #'tenure' : self.tenure,
                    #                #'title' : self.title,
                    #                'issue_date' : timezone.now(),
                    #                'expiry_date' : details.get('expiry_date'),
                    #                'start_date' : details.get('start_date'),
                    #                'applicant' : self.proposal.applicant,
                    #                'proxy_applicant' : self.proposal.proxy_applicant,
                    #                'lodgement_number': previous_approval.lodgement_number,
                    #                'apiary_approval': self.proposal.apiary_group_application_type,
                    #                #'extracted_fields' = JSONField(blank=True, null=True)
                    #            }
                    #        )
                    #        if created:
                    #            previous_approval.replaced_by = approval
                    #            previous_approval.save()

                    elif self.proposal.proposal_type == 'amendment':
                        # TODO - fix for apiary approval
                        pass
                    #    if self.proposal.previous_application:
                    #        previous_approval = self.proposal.previous_application.approval
                    #        approval,created = Approval.objects.update_or_create(
                    #            current_proposal = checking_proposal,
                    #            defaults = {
                    #                #'activity' : self.activity,
                    #                #'region' : self.region,
                    #                #'tenure' : self.tenure,
                    #                #'title' : self.title,
                    #                'issue_date' : timezone.now(),
                    #                'expiry_date' : details.get('expiry_date'),
                    #                'start_date' : details.get('start_date'),
                    #                'applicant' : self.proposal.applicant,
                    #                'proxy_applicant' : self.proposal.proxy_applicant,
                    #                'lodgement_number': previous_approval.lodgement_number,
                    #                'apiary_approval': self.proposal.apiary_group_application_type,
                    #                #'extracted_fields' = JSONField(blank=True, null=True)
                    #            }
                    #        )
                    #        if created:
                    #            previous_approval.replaced_by = approval
                    #            previous_approval.save()
                    #            # Get apiary sites from proposal
                    #            #if self.proposal.application_type == ApplicationType.APIARY:
                    #            #    for site in self.apiary_sites.all():
                    #            #        site.approval = approval
                    #            #elif self.proposal.application_type == ApplicationType.SITE_TRANSFER:
                    #            #    for site in self.apiary_site_transfer.apiary_sites.all():
                    #            #        site.approval = approval
                    #            for site in self.apiary_sites.all():
                    #                site.approval = approval

                    #import ipdb; ipdb.set_trace()
                    if self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                        # approval must already exist - we reissue with same start and expiry dates
                        # does thhis need to be reissued with self.reissue_approval() ?
                        originating_approval.issue_date = timezone.now()
                        originating_approval.current_proposal = checking_proposal
                        if originating_approval.reissued:
                            originating_approval.expiry_date = details.get('expiry_date')
                            originating_approval.start_date = details.get('start_date')
                        # always reset this flag
                        originating_approval.reissued = False
                        #self.proposal.proposed_issuance_approval['start_date'] = originating_approval.start_date.strftime('%d/%m/%Y')
                        #self.proposal.proposed_issuance_approval['expiry_date'] = originating_approval.expiry_date.strftime('%d/%m/%Y')
                        #self.proposal.proposed_issuance_approval['details'] = ''
                        #self.proposal.proposed_issuance_approval['cc_email'] = ''
                        originating_approval.save()
                        target_approval.issue_date = timezone.now()
                        #target_approval.current_proposal = checking_proposal
                        target_approval.save()
                    # Apiary approval
                    else:
                        if not approval:
                            # There are no existing approvals.  Create a new one.
                            approval, created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                #'activity' : self.activity,
                                #'region' : self.region.name,
                                #'tenure' : self.tenure.name,
                                #'title' : self.title,
                                'issue_date' : timezone.now(),
                                'expiry_date' : details.get('expiry_date'),
                                'start_date' : details.get('start_date'),
                                'applicant' : self.proposal.applicant,
                                'proxy_applicant' : self.proposal.proxy_applicant,
                                'apiary_approval': self.proposal.apiary_group_application_type,
                                #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                        else:
                            approval.issue_date = timezone.now()
                            # ensure current_proposal is updated with this proposal
                            #if self.proposal.application_type.name != ApplicationType.SITE_TRANSFER:
                            approval.current_proposal = checking_proposal
                            if approval.reissued:
                                approval.expiry_date = details.get('expiry_date')
                                approval.start_date = details.get('start_date')
                            elif self.proposal.proposal_type == 'renewal':
                                approval.expiry_date = details.get('expiry_date')
                            # always reset this flag
                            approval.reissued = False
                            #self.proposal.proposed_issuance_approval['start_date'] = approval.start_date.strftime('%d/%m/%Y')
                            #self.proposal.proposed_issuance_approval['expiry_date'] = approval.expiry_date.strftime('%d/%m/%Y')
                            #self.proposal.proposed_issuance_approval['details'] = ''
                            #self.proposal.proposed_issuance_approval['cc_email'] = ''
                            approval.save()


                    # Get apiary sites from proposal
                    #if self.proposal.application_type == ApplicationType.APIARY:
                    #    for site in self.proposal_apiary.apiary_sites.all():
                    #        site.approval = approval
                    #elif self.proposal.application_type == ApplicationType.SITE_TRANSFER:
                    #    for site in self.apiary_site_transfer.apiary_sites.all():
                    #        site.approval = approval
                    #import ipdb;ipdb.set_trace()
                    # for site in self.apiary_sites.all():
                    if self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                        #import ipdb; ipdb.set_trace()
                        # updated apiary_site.selected with 'checked' flag status
                        apiary_sites = request.data.get('apiary_sites', [])
                        for apiary_site in apiary_sites:
                            transfer_site = SiteTransferApiarySite.objects.get(
                                    proposal_apiary=self,
                                    apiary_site_on_approval__apiary_site_id=apiary_site.get('id')
                                    )
                            transfer_site.internal_selected = apiary_site.get('checked') if transfer_site.customer_selected else False
                            transfer_site.save()
                        # update approval for all selected apiary sites
                        transfer_sites = SiteTransferApiarySite.objects.filter(
                                proposal_apiary=self,
                                internal_selected=True,
                                customer_selected=True
                                )
                        for site_transfer_apiary_site in transfer_sites:
                            relation_original = site_transfer_apiary_site.apiary_site_on_approval
                            from disturbance.components.approvals.models import ApiarySiteOnApproval
                            relation_target, created = ApiarySiteOnApproval.objects.get_or_create(
                                apiary_site=relation_original.apiary_site,
                                approval=target_approval,
                            )
                            relation_target.site_status = relation_original.site_status  # Copy the site status from the original to the target
                            relation_original.site_status = SITE_STATUS_TRANSFERRED  # Set the site status of the original site to 'transferred'
                            relation_original.save()
                            relation_target.wkb_geometry = relation_original.wkb_geometry
                            relation_target.site_category = relation_original.site_category
                            relation_target.save()
                    else:
                        count_approved_site = 0
                        sites_received = request.data.get('apiary_sites', [])

                        # Check the number of sites to be approved
                        sites_approved = [site for site in sites_received if site['checked']]
                        if len(sites_approved) == 0:
                            raise ValidationError("There must be at least one apiary site to approve")

                        self._update_apiary_sites(approval, sites_received, request)

                        # Check the current annual rental fee period
                        # Determine the start and end date of the annual rental fee, for which the invoices should be issued
                        today_now_local = datetime.datetime.now(pytz.timezone(TIME_ZONE))
                        today_date_local = today_now_local.date()
                        from disturbance.management.commands.send_annual_rental_fee_invoice import get_annual_rental_fee_period
                        period_start_date, period_end_date = get_annual_rental_fee_period(today_date_local)

                        # Retrieve annual rental fee period object for the period calculated above
                        # This period should not overwrap the existings, otherwise you will need a refund
                        from disturbance.components.das_payments.models import AnnualRentalFeePeriod
                        annual_rental_fee_period, created = AnnualRentalFeePeriod.objects.get_or_create(period_start_date=period_start_date, period_end_date=period_end_date)

                        from disturbance.components.das_payments.utils import \
                            generate_line_items_for_annual_rental_fee
                        products, details_dict = generate_line_items_for_annual_rental_fee(approval, today_now_local, (period_start_date, period_end_date), sites_approved)
                        from disturbance.management.commands.send_annual_rental_fee_invoice import make_serializable
                        products = make_serializable(products)  # Make line items serializable to store in the JSONField

                        from disturbance.components.das_payments.models import AnnualRentalFee
                        annual_rental_fee = AnnualRentalFee.objects.create(
                            approval=approval,
                            annual_rental_fee_period=annual_rental_fee_period,
                            # invoice_reference=invoice.reference,  #  We don't create an ledger invoice, but just store the lines
                            invoice_period_start_date=details_dict['charge_start_date'],
                            invoice_period_end_date=details_dict['charge_end_date'],
                            lines=products,  # This is used when generating the invoice at payment time
                        )

                        for site in sites_approved:
                            # Store the apiary sites which the invoice created above has been issued for
                            apiary_site = ApiarySite.objects.get(id=site['id'])
                            from disturbance.components.das_payments.models import AnnualRentalFeeApiarySite
                            annual_rental_fee_apiary_site = AnnualRentalFeeApiarySite(apiary_site=apiary_site, annual_rental_fee=annual_rental_fee)
                            annual_rental_fee_apiary_site.save()

                            # Add approved sites to the existing temporary use proposal with status 'draft'
                            proposal_apiary_temporary_use_qs = ProposalApiaryTemporaryUse.objects.filter(loaning_approval=approval, proposal__processing_status=Proposal.PROCESSING_STATUS_DRAFT)
                            for proposal_apiary_temporary_use in proposal_apiary_temporary_use_qs:
                                temp_use_apiary_site, created = TemporaryUseApiarySite.objects.get_or_create(apiary_site=site, proposal_apiary_temporary_use=proposal_apiary_temporary_use)

                        from disturbance.components.approvals.email import \
                            send_annual_rental_fee_awaiting_payment_confirmation
                        email_data = send_annual_rental_fee_awaiting_payment_confirmation(approval, annual_rental_fee)
                        # TODO: Add comms log

                        #print approval,approval.id, created
                    # Generate compliances
                    #self.generate_compliances(approval, request)
                    if self.proposal.application_type.name == ApplicationType.APIARY:
                        from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                        if created:
                            # ProposalType set during def create api method
                            #if self.proposal.proposal_type == 'amendment':
                            ## Delete future dated compliances for approval

                            #if self.proposal.previous_application:
                            #    approval_compliances = Compliance.objects.filter(
                            #            approval= approval, 
                            #            #proposal = self.proposal.previous_application, 
                            #            processing_status='future'
                            #            )
                            #    if approval_compliances:
                            #        for c in approval_compliances:
                            #            c.delete()

                            # Log creation
                            # Generate the document
                            approval.generate_doc(request.user)
                            self.link_apiary_approval_requirements(approval)
                            self.generate_apiary_compliances(approval, request)
                            # send the doc and log in approval and org
                        else:
                            #approval.replaced_by = request.user
                            #approval.replaced_by = self.approval
                            # Generate the document
                            approval.generate_doc(request.user)
                            #Delete the future compliances if Approval is reissued and generate the compliances again.
                            approval_compliances = Compliance.objects.filter(
                                    approval= approval, 
                                    #proposal = self.proposal, 
                                    processing_status='future'
                                    )
                            if approval_compliances:
                                for c in approval_compliances:
                                    c.delete()
                            self.link_apiary_approval_requirements(approval)
                            self.generate_apiary_compliances(approval, request)
                            # Log proposal action
                            self.proposal.log_user_action(
                                    ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                    request
                                    )
                            # Log entry for organisation
                            if self.proposal.applicant:
                                self.proposal.applicant.log_user_action(
                                        ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                        request
                                        )
                        self.proposal.approval = approval
                        #send Proposal approval email with attachment
                        send_proposal_approval_email_notification(self.proposal,request)
                        self.proposal.save(version_comment='Final Approval: {}'.format(self.proposal.approval.lodgement_number))
                        self.proposal.approval.documents.all().update(can_delete=False)
                    elif self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                        #import ipdb;ipdb.set_trace()
                        # add Site Transfer Compliance/Requirements logic here
                        from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                        ## Originating approval
                        originating_approval.generate_apiary_site_transfer_doc(request.user, site_transfer_proposal=self.proposal)
                        #Delete the future compliances if Approval is reissued and generate the compliances again.
                        approval_compliances = Compliance.objects.filter(
                                approval= originating_approval,
                                #proposal = self.proposal, 
                                processing_status='future'
                                )
                        if approval_compliances:
                            for c in approval_compliances:
                                c.delete()
                        #self.generate_apiary_site_transfer_compliances(originating_approval, request)
                        self.link_apiary_approval_requirements(originating_approval)
                        self.generate_apiary_compliances(originating_approval, request)
                        # Log proposal action
                        self.proposal.log_user_action(
                                ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                request
                                )
                        # Log entry for organisation
                        if self.proposal.applicant:
                            self.proposal.applicant.log_user_action(
                                    ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                    request
                                    )
                        #send Proposal approval email with attachment
                        send_site_transfer_approval_email_notification(self.proposal, request, originating_approval)
                        ## Target approval
                        target_approval.generate_apiary_site_transfer_doc(request.user, site_transfer_proposal=self.proposal)
                        #Delete the future compliances if Approval is reissued and generate the compliances again.
                        approval_compliances = Compliance.objects.filter(
                                approval= target_approval,
                                #proposal = self.proposal, 
                                processing_status='future'
                                )
                        if approval_compliances:
                            for c in approval_compliances:
                                c.delete()
                        self.link_apiary_approval_requirements(target_approval)
                        self.generate_apiary_compliances(target_approval, request)
                        # Log proposal action
                        self.proposal.log_user_action(
                                ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                request
                                )
                        # Log entry for organisation
                        if self.proposal.applicant:
                            self.proposal.applicant.log_user_action(
                                    ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.id),
                                    request
                                    )
                        #send Proposal approval email with attachment
                        send_site_transfer_approval_email_notification(self.proposal, request, target_approval)
                        #self.proposal.save(version_comment='Final Approval: {}'.format(self.proposal.approval.lodgement_number))
                        self.proposal.save(version_comment='Originating Approval: {}, Target Approval: {}'.format(
                            originating_approval.lodgement_number,
                            target_approval.lodgement_number,
                            )
                        )
                        #self.proposal.approval.documents.all().update(can_delete=False)
                        originating_approval.documents.all().update(can_delete=False)
                        target_approval.documents.all().update(can_delete=False)

            except:
                raise

    def link_apiary_approval_requirements(self, approval):
        # Ensure current requirements are associated with apiary approval
        link_requirement_set = self.proposal.requirements.all()
        for link_r in link_requirement_set:
            link_r.apiary_approval = approval
            link_r.save()
        # Remove apiary approval link from previous requirements
        unlink_requirement_set = approval.proposalrequirement_set.exclude(proposal=self.proposal)
        for unlink_r in unlink_requirement_set:
            unlink_r.apiary_approval = None
            unlink_r.save()

    def generate_apiary_compliances(self,approval, request):
        #import ipdb; ipdb.set_trace()
        today = timezone.now().date()
        timedelta = datetime.timedelta
        from disturbance.components.compliances.models import Compliance, ComplianceUserAction

        proposal = self.proposal
        #For amendment type of Proposal, check for copied requirements from previous proposal
        #if proposal.proposal_type == 'amendment':
        if self.proposal.previous_application:
            try:
                for r in proposal.requirements.filter(apiary_approval=approval).filter(copied_from__isnull=False):
                    cs=[]
                    # Now discard all of the due compliances
                    cs=Compliance.objects.filter(
                            requirement=r.copied_from, 
                            approval=approval, 
                            processing_status='due'
                            )
                    if cs:
                        if r.is_deleted:
                            for c in cs:
                                c.processing_status='discarded'
                                c.customer_status = 'discarded'
                                c.reminder_sent=True
                                c.post_reminder_sent=True
                                c.save()
                        #if r.is_deleted == False:
                        #    for c in cs:
                        #        #c.proposal= proposal
                        #        c.approval=approval
                        #        c.requirement=r
                        #        c.save()
            except:
                raise
        #requirement_set= self.requirements.filter(copied_from__isnull=True).exclude(is_deleted=True)
        requirement_set= proposal.requirements.filter(apiary_approval=approval).exclude(is_deleted=True)

        #for req in self.requirements.all():
        for req in requirement_set:
            try:
                if req.due_date and req.due_date >= today:
                    current_date = req.due_date
                    #create a first Compliance
                    try:
                        compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                    except Compliance.DoesNotExist:
                        compliance =Compliance.objects.create(
                                    #proposal=proposal,
                                    due_date=current_date,
                                    processing_status='future',
                                    approval=approval,
                                    requirement=req,
                                    apiary_compliance=True
                        )
                        compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
                    if req.recurrence:
                        while current_date < approval.expiry_date:
                            for x in range(req.recurrence_schedule):
                            #Weekly
                                if req.recurrence_pattern == 1:
                                    current_date += timedelta(weeks=1)
                            #Monthly
                                elif req.recurrence_pattern == 2:
                                    current_date += timedelta(weeks=4)
                                    pass
                            #Yearly
                                elif req.recurrence_pattern == 3:
                                    current_date += timedelta(days=365)
                            # Create the compliance
                            if current_date <= approval.expiry_date:
                                try:
                                    compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                                except Compliance.DoesNotExist:
                                    compliance =Compliance.objects.create(
                                                #proposal=self,
                                                due_date=current_date,
                                                processing_status='future',
                                                approval=approval,
                                                requirement=req,
                                                apiary_compliance=True
                                    )
                                    compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
            except:
                raise

    def _update_apiary_sites(self, approval, sites_approved, request):
        for my_site in sites_approved:
            a_site = ApiarySite.objects.get(id=my_site['id'])
            apiary_site_on_proposal = self.get_relation(a_site)

            if my_site['checked']:
                # relation.approval = approval
                apiary_site_on_proposal.site_status = SITE_STATUS_APPROVED
            else:
                apiary_site_on_proposal.site_status = SITE_STATUS_DENIED
            # Reset selected status to make the checkboxes unticked when renewal or so
            apiary_site_on_proposal.workflow_selected_status = False
            apiary_site_on_proposal.save()

            # Apiary Site can be moved by assessor and/or approver
            if 'coordinates_moved' in my_site:
                prev_coordinates = apiary_site_on_proposal.wkb_geometry_processed.get_coords()
                geom_str = GEOSGeometry('POINT(' + str(my_site['coordinates_moved']['lng']) + ' ' + str(my_site['coordinates_moved']['lat']) + ')', srid=4326)
                from disturbance.components.proposals.serializers_apiary import ApiarySiteOnProposalProcessedGeometrySaveSerializer
                serializer = ApiarySiteOnProposalProcessedGeometrySaveSerializer(apiary_site_on_proposal, data={'wkb_geometry_processed': geom_str})
                serializer.is_valid(raise_exception=True)
                serializer.save()

                # Log it
                self.proposal.log_user_action(ProposalUserAction.APIARY_SITE_MOVED.format(my_site['id'], prev_coordinates, (my_site['coordinates_moved']['lng'], my_site['coordinates_moved']['lat'])), request)

            # Because this is final approval, copy the data from the proposal to the approval
            from disturbance.components.approvals.models import ApiarySiteOnApproval
            if apiary_site_on_proposal.site_status == SITE_STATUS_APPROVED:
                # Create a relation between the approved apairy site and the approval
                apiary_site_on_approval, created = ApiarySiteOnApproval.objects.get_or_create(apiary_site=a_site, approval=approval)
                apiary_site_on_approval.wkb_geometry = apiary_site_on_proposal.wkb_geometry_processed
                apiary_site_on_approval.site_category = apiary_site_on_proposal.site_category_processed
                apiary_site_on_approval.site_status = SITE_STATUS_CURRENT
                apiary_site_on_approval.save()


class SiteCategory(models.Model):
    CATEGORY_SOUTH_WEST = 'south_west'
    CATEGORY_REMOTE = 'remote'
    CATEGORY_CHOICES = (
        (CATEGORY_SOUTH_WEST, 'South West'),
        (CATEGORY_REMOTE, 'Remote')
    )
    # This model is used to distinguish the application gtfees' differences
    name = models.CharField(unique=True, max_length=50, choices=CATEGORY_CHOICES)

    def retrieve_current_fee_per_site_by_type(self, fee_type_name):
        today_local = datetime.datetime.now(pytz.timezone(TIME_ZONE)).date()
        ret_date = self.retrieve_fee_by_date_and_type(today_local, fee_type_name)
        return ret_date

    def retrieve_fee_by_date_and_type(self, target_date, fee_type_name):
        fee_type_application = ApiarySiteFeeType.objects.get(name=fee_type_name)
        if not fee_type_application:
            raise Exception("Please select 'new_application' and save it at the Apiary Site Fee Type admin page")

        site_fee = ApiarySiteFee.objects.filter(
                    Q(apiary_site_fee_type=fee_type_application) &
                    Q(site_category=self) &
                    Q(date_of_enforcement__lte=target_date)
                    ).order_by('date_of_enforcement', ).last()

        if site_fee:
            return site_fee.amount
        else:
            return None

    @property
    def display_name(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if self.name == item[0]:
                return item[1]
        return '---'

    def __str__(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if item[0] == self.name:
                fee_application = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_APPLICATION)
                # fee_amendment = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_AMENDMENT)
                fee_renewal = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_RENEWAL)
                fee_transfer = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_TRANSFER)
                return '{} - new application: ${}, transfer: ${}, renewal: ${}'.format(item[1], fee_application, fee_transfer, fee_renewal)
        return '---'

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'apiary site fee'


class ApiarySiteFeeType(RevisionedMixin):
    FEE_TYPE_APPLICATION = 'new_application'
    # FEE_TYPE_AMENDMENT = 'amendment'
    FEE_TYPE_RENEWAL = 'renewal'
    FEE_TYPE_TRANSFER = 'transfer'
    FEE_TYPE_CHOICES = (
        (FEE_TYPE_APPLICATION, 'New Application'),
        # (FEE_TYPE_AMENDMENT, 'Amendment'),
        (FEE_TYPE_RENEWAL, 'Renewal'),
        (FEE_TYPE_TRANSFER, 'Transfer'),
    )
    name = models.CharField(unique=True, max_length=50, choices=FEE_TYPE_CHOICES,)
    description = models.TextField(blank=True)

    def __str__(self):
        for item in ApiarySiteFeeType.FEE_TYPE_CHOICES:
            if item[0] == self.name:
                return '{}'.format(item[1])
        return '---'

    class Meta:
        app_label = 'disturbance'


class ApiarySiteFee(RevisionedMixin):
    amount = models.DecimalField(max_digits=8, decimal_places=2, default='0.00')
    date_of_enforcement = models.DateField(blank=True, null=True)
    site_category = models.ForeignKey(SiteCategory, related_name='site_fees')
    apiary_site_fee_type = models.ForeignKey(ApiarySiteFeeType, null=True, blank=True)

    class Meta:
        app_label = 'disturbance'
        ordering = ('date_of_enforcement', )  # oldest record first, latest record last

    def __str__(self):
        return '${} ({}:{})'.format(self.amount, self.date_of_enforcement, self.site_category)


class ApiaryAnnualRentalFee(RevisionedMixin):
    """
    This amount is applied from the date_from
    """
    amount = models.DecimalField(max_digits=8, decimal_places=2, default='0.00')
    date_from = models.DateField(blank=True, null=True)

    class Meta:
        app_label = 'disturbance'
        ordering = ('date_from', )  # oldest record first, latest record last

    def __str__(self):
        return 'Amount: ${}: From: {}'.format(self.amount, self.date_from)

    @staticmethod
    def get_fee_at_target_date(target_date):
        fee_applied = ApiaryAnnualRentalFee.objects.filter(date_from__lte=target_date).order_by('-date_from').first()
        return fee_applied


class ApiaryAnnualRentalFeePeriodStartDate(RevisionedMixin):
    """
    Calculation of the annual rental fee starts from this date
    """
    NAME_PERIOD_START = 'period_start_date'
    NAME_CHOICES = (
        (NAME_PERIOD_START, 'Start date of the annual rental fee'),
    )
    name = models.CharField(unique=True, max_length=50, choices=NAME_CHOICES, )
    period_start_date = models.DateField(blank=True, null=True)

    def __str__(self):
        return '{}: {} {}'.format(self.name, self.period_start_date.strftime('%B'), self.period_start_date.day)

    class Meta:
        app_label = 'disturbance'
        ordering = ('period_start_date', )  # oldest record first, latest record last


class ApiaryAnnualRentalFeeRunDate(RevisionedMixin):
    """
    This is the date to issue the annual rental fee invoices
    """
    NAME_CRON = 'date_to_run_cron_job'
    NAME_CHOICES = (
        (NAME_CRON, 'Date to Issue'),
    )
    name = models.CharField(unique=True, max_length=50, choices=NAME_CHOICES, )
    date_run_cron = models.DateField(blank=True, null=True)

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Annual Rental Fee Issue Date'

    def __str__(self):
        return '{}: {} {}'.format(self.name, self.date_run_cron.strftime('%B'), self.date_run_cron.day)


class ApiarySite(models.Model):
    site_guid = models.CharField(max_length=50, blank=True)
    latest_proposal_link = models.ForeignKey('disturbance.ApiarySiteOnProposal', blank=True, null=True, on_delete=models.SET_NULL)
    latest_approval_link = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True, on_delete=models.SET_NULL)
    # Store the proposal link intermediate object this apiary site transitioned from when got the 'vacant' status
    proposal_link_for_vacant = models.ForeignKey('disturbance.ApiarySiteOnProposal', blank=True, null=True, related_name='vacant_apiary_site', on_delete=models.SET_NULL)
    # Store the approval link intermediate object this apiary site transitioned from when got the 'vacant' status
    approval_link_for_vacant = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True, related_name='vacant_apiary_site', on_delete=models.SET_NULL)
    is_vacant = models.BooleanField(default=False)

    def __str__(self):
        return '{}'.format(self.id,)
    
    def delete(self, using=None, keep_parents=False):
        super(ApiarySite, self).delete(using, keep_parents)
        print('ApiarySite: {}({}) has been deleted.'.format(self.id, self.is_vacant))

    def make_vacant(self, vacant, relation):
        from disturbance.components.approvals.models import ApiarySiteOnApproval
        if isinstance(relation, ApiarySiteOnProposal):
            self.is_vacant = vacant
            self.proposal_link_for_vacant = relation if vacant else None
            self.approval_link_for_vacant = None
        elif isinstance(relation, ApiarySiteOnApproval):
            self.is_vacant = vacant
            self.proposal_link_for_vacant = None
            self.approval_link_for_vacant = relation if vacant else None
        self.save()

    def get_relation(self, proposal_apiary_or_approval):
        if isinstance(proposal_apiary_or_approval, ProposalApiary):
            return ApiarySiteOnProposal.objects.get(apiary_site=self, proposal_apiary=proposal_apiary_or_approval)
        else:
            from disturbance.components.approvals.models import ApiarySiteOnApproval
            return ApiarySiteOnApproval.objects.get(apiary_site=self, approval=proposal_apiary_or_approval)

    def get_current_application_fee_per_site(self):
        current_fee = self.site_category.current_application_fee_per_site
        return current_fee

    class Meta:
        app_label = 'disturbance'


# class ApiarySiteLocation(models.Model):
#     TYPE_DRAFT = 'draft'
#     TYPE_PROCESSED = 'processed'
#     TYPE_APPROVED = 'approved'
#     TYPE_CHOICES = (
#         (TYPE_DRAFT, 'Draft'),
#         (TYPE_PROCESSED, 'Processed'),
#         (TYPE_APPROVED, 'Approved'),
#     )
#     wkb_geometry = PointField(srid=4326, blank=True, null=True)
#     type = models.CharField(max_length=40, choices=TYPE_CHOICES, default=TYPE_CHOICES[0][0])
#     apiary_site = models.ForeignKey('ApiarySite', blank=True, null=True)
#     proposal_apiary = models.ForeignKey('ProposalApiary', blank=True, null=True)
#     approval = models.ForeignKey('Approval', blank=True, null=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#     modified_at = models.DateTimeField(auto_now=True)
#     objects = GeoManager()
#
#     class Meta:
#         app_label = 'disturbance'
#         ordering = ['-modified_at', '-created_at',]


class ApiarySiteFeeRemainder(models.Model):
    '''
    A record of this model represents e site is left

    You have to check the validity of the record by date_expiry and date_used fields
    '''
    site_category = models.ForeignKey(SiteCategory)
    apiary_site_fee_type = models.ForeignKey(ApiarySiteFeeType)
    applicant = models.ForeignKey(Organisation, null=True, blank=True)
    proxy_applicant = models.ForeignKey(EmailUser, null=True, blank=True)
    datetime_created = models.DateTimeField(auto_now_add=True)
    date_expiry = models.DateField(null=True, blank=True)
    date_used = models.DateField(null=True, blank=True)

    def __str__(self):
        #return 'Remainder: {} - {} - {} - {} site(s)'.format(self.applicant, self.site_category, self.apiary_site_fee_type, self.number_of_sites_left)
        return 'Remainder: {} - {} - {} - site(s)'.format(self.applicant, self.site_category, self.apiary_site_fee_type)

    class Meta:
        app_label = 'disturbance'


class OnSiteInformation(models.Model):
    # apiary_site = models.ForeignKey(ApiarySite, null=True, blank=True)
    apiary_site_on_approval = models.ForeignKey('ApiarySiteOnApproval', blank=True, null=True)
    period_from = models.DateField(null=True, blank=True)
    period_to = models.DateField(null=True, blank=True)
    comments = models.TextField(blank=True)
    datetime_deleted = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return 'OnSiteInfo id: {}, date: {} to {}'.format(self.id, self.period_from, self.period_to)

    class Meta:
        app_label = 'disturbance'


class ProposalApiaryTemporaryUse(models.Model):
    from_date = models.DateField('Period From Date', blank=True, null=True)
    to_date = models.DateField('Period To Date', blank=True, null=True)
    proposal = models.OneToOneField(Proposal, related_name='apiary_temporary_use', null=True, blank=True)
    # proposal_apiary_base = models.ForeignKey(Proposal, related_name='apiary_temporary_use_set', null=True, blank=True)
    temporary_occupier_name = models.CharField(max_length=255, blank=True, null=True)
    temporary_occupier_phone = models.CharField(max_length=50, blank=True, null=True)
    temporary_occupier_mobile = models.CharField(max_length=50, blank=True, null=True)
    temporary_occupier_email = models.EmailField(blank=True, null=True)
    loaning_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True)

    def __str__(self):
        if self.proposal.proposal_apiary:
            return 'id:{} - {}'.format(self.id, self.proposal.proposal_apiary.title)
        else:
            # Should not reach here
            return 'id:{}'.format(self.id)

    class Meta:
        app_label = 'disturbance'

    def period_valid_for_temporary_use(self, period):
        detail = {}
        valid = True

        # Check if the period sits in the approval valid period
        if period[0] < self.loaning_approval.start_date or self.loaning_approval.expiry_date < period[1]:
            valid = False
            if not valid:
                detail['period'] = {}
                detail['period']['from_date'] = self.loaning_approval.start_date
                detail['period']['to_date'] = self.loaning_approval.expiry_date
                detail['reason'] = 'out_of_range_of_licence'
                return valid, detail

        # TODO: Check if the period submitted overlaps with the existing temprary use periods
        #qs = TemporaryUseApiarySite.objects.filter(apiary_site=self, selected=True, proposal_apiary_temporary_use__proposal__processing_status=Proposal.PROCESSING_STATUS_APPROVED)
        #for temp_site in qs:
        #    valid = (period[0] <= period[1] < temp_site.proposal_apiary_temporary_use.from_date) or (temp_site.proposal_apiary_temporary_use.to_date < period[0] <= period[1])
        #    if not valid:
        #        detail['period'] = {}
        #        detail['period']['from_date'] = temp_site.proposal_apiary_temporary_use.from_date
        #        detail['period']['to_date'] = temp_site.proposal_apiary_temporary_use.to_date
        #        detail['apiary_site'] = temp_site.apiary_site
        #        detail['reason'] = 'overlap_existing'
        #        return valid, detail

        return valid, detail


class TemporaryUseApiarySite(models.Model):
    """
    Apiary sites under a proposal can be partially used as temporary site
    """
    proposal_apiary_temporary_use = models.ForeignKey(ProposalApiaryTemporaryUse, blank=True, null=True, related_name='temporary_use_apiary_sites')
    # apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True)
    apiary_site_on_approval = models.ForeignKey('ApiarySiteOnApproval', blank=True, null=True)
    selected = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'


class SiteTransferApiarySite(models.Model):
    proposal_apiary = models.ForeignKey(ProposalApiary, blank=True, null=True, related_name='site_transfer_apiary_sites')
    # apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True)
    apiary_site_on_approval = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True)
    internal_selected = models.BooleanField(default=False)
    customer_selected = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'


# TODO: remove if no longer required
class ApiarySiteApproval(models.Model):
    """
    This is intermediate table between ApiarySite and Approval to hold an approved apiary site under a certain approval
    """
    apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True, related_name='apiary_site_approval_set')
    approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name='apiary_site_approval_set')

    class Meta:
        app_label = 'disturbance'


# TODO: remove if no longer required
class ProposalApiarySiteTransfer(models.Model):
    email = models.EmailField('Email of Transferee', max_length=254, blank=True, null=True)
    proposal = models.OneToOneField(Proposal, related_name='apiary_site_transfer', null=True)
    transferee = models.ForeignKey(EmailUser, blank=True, null=True, related_name='transferee')

    def __str__(self):
        if self.proposal.proposal_apiary:
            return 'id:{} - {}'.format(self.id, self.proposal.proposal_apiary.title)
        else:
            # Should not reach here
            return 'id:{}'.format(self.id)

    #def __str__(self):
     #   return '{}'.format(self.title)

    class Meta:
        app_label = 'disturbance'


class ProposalApiaryDocument(DefaultDocument):
    proposal = models.ForeignKey('Proposal', related_name='apiary_documents')
    _file = models.FileField(upload_to=update_apiary_doc_filename, max_length=512)

    def delete(self):
        if self.can_delete:
            return super(ProposalApiaryDocument, self).delete()

class DeedPollDocument(Document):
    proposal = models.ForeignKey(ProposalApiary, related_name='deed_poll_documents', blank=True, null=True)
    base_proposal = models.ForeignKey(Proposal, related_name='deed_poll_documents', blank=True, null=True)
    _file = models.FileField(max_length=255)
    input_name = models.CharField(max_length=255, blank=True, null=True)
    # after initial submit prevent document from being deleted
    can_delete = models.BooleanField(default=True)
    #version_comment = models.CharField(max_length=255, blank=True, null=True)
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(DeedPollDocument, self).delete()

    class Meta:
        app_label = 'disturbance'

#class DeedPollDocument(DefaultDocument):
#    proposal = models.ForeignKey('Proposal', related_name='deed_poll_documents')
#    _file = models.FileField(max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(DeedPollDocument, self).delete()


class ApiaryChecklistQuestion(models.Model):
    ANSWER_TYPE_CHOICES = (
        ('yes_no', 'Yes/No type'),
        ('free_text','Free text type'),
    )
    CHECKLIST_TYPE_CHOICES = (
        ('apiary', 'Apiary'),
        ('site_transfer', 'Site Transfer'),
    )
    CHECKLIST_ROLE_CHOICES = (
        ('assessor', 'Assessor'),
        ('applicant', 'Applicant'),
        ('referrer', 'Referrer'),
    )
    text = models.TextField()
    answer_type = models.CharField('Answer Type',
                                   max_length=30,
                                   choices=ANSWER_TYPE_CHOICES,
                                   default=ANSWER_TYPE_CHOICES[0][0])
    checklist_type = models.CharField('Checklist Type',
                                   max_length=30,
                                   choices=CHECKLIST_TYPE_CHOICES,
                                   #default=ANSWER_TYPE_CHOICES[0][0]
                                   )
    checklist_role = models.CharField('Checklist Role',
                                   max_length=30,
                                   choices=CHECKLIST_ROLE_CHOICES,
                                   #default=ANSWER_TYPE_CHOICES[0][0]
                                   )
    order = models.PositiveIntegerField(default=1)

    def __str__(self):
        return self.text

    class Meta:
        app_label = 'disturbance'
        ordering = ['order', 'id']


class ApiaryChecklistAnswer(models.Model):
    question=models.ForeignKey(ApiaryChecklistQuestion, related_name='answers')
    answer = models.NullBooleanField()
    proposal = models.ForeignKey(ProposalApiary, related_name="apiary_checklist")
    apiary_referral = models.ForeignKey('ApiaryReferral', related_name="apiary_checklist_referral", blank=True, null=True)
    text_answer= models.CharField(max_length=256, blank=True, null=True)

    def __str__(self):
        return self.question.text

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'CheckList answer'
        verbose_name_plural = 'CheckList answers'

#class ApiaryTemporaryUseDocument(DefaultDocument):
#    temporary_use = models.ForeignKey('ProposalApiaryTemporaryUse', related_name='apiary_temporary_use_documents')
#    _file = models.FileField(upload_to=update_temporary_use_doc_filename, max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(ApiarySiteLocationDocument, self).delete()
#
#class ApiarySiteTransferDocument(DefaultDocument):
#    site_transfer = models.ForeignKey('ProposalApiarySiteTransfer', related_name='apiary_site_transfer_documents')
#    _file = models.FileField(upload_to=update_site_transfer_doc_filename, max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(ApiarySiteLocationDocument, self).delete()


class ApiaryAssessorGroup(models.Model):
    #site = models.OneToOneField(Site, default='1') 
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        return 'Apiary Assessors Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    class Meta:
        app_label = 'disturbance'
        verbose_name_plural = 'Apiary Assessors Group'

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]


class ApiaryApproverGroup(models.Model):
    #site = models.OneToOneField(Site, default='1') 
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        return 'Apiary Approvers Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    class Meta:
        app_label = 'disturbance'
        verbose_name_plural = 'Apiary Approvers Group'

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]



class ApiaryReferral(RevisionedMixin):
    #SENT_CHOICES = (
    #    (1,'Sent From Assessor'),
    #    (2,'Sent From Referral')
    #)
    #PROCESSING_STATUS_CHOICES = (
    #                             ('with_referral', 'Awaiting'),
    #                             ('recalled', 'Recalled'),
    #                             ('completed', 'Completed'),
    #                             )
    #lodged_on = models.DateTimeField(auto_now_add=True)
    #proposal = models.ForeignKey(ProposalApiary,related_name='referrals')
    #sent_by = models.ForeignKey(EmailUser,related_name='disturbance_apiary_assessor_referrals')
    #referral = models.ForeignKey(EmailUser,null=True,blank=True,related_name='disturbance_apiary_referalls')
    #referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='referral_groups')
    #linked = models.BooleanField(default=False)
    #sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    #processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
    #                                     default=PROCESSING_STATUS_CHOICES[0][0])
    #text = models.TextField(blank=True) #Assessor text
    #referral_text = models.TextField(blank=True)
    ## is document required?
    ##document = models.ForeignKey(ReferralDocument, blank=True, null=True, related_name='referral_document')

    referral = models.OneToOneField(Referral, related_name='apiary_referral', null=True)
    referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='referral_groups')

    class Meta:
        app_label = 'disturbance'
        #ordering = ('-lodged_on',)

    def __str__(self):
        return 'Apiary Application {} - Referral {}'.format(
                self.referral.proposal.id,
                self.referral.id
                )

    # Methods
    #@property
    #def latest_referrals(self):
     #   return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[:2]

    #@property
    #def referral_assessment(self):
     #   qs=self.assessment.filter(referral_assessment=True, referral_group=self.referral_group)
      #  if qs:
       #     return qs[0]
        #else:
         #   return None

    #@property
    #def can_be_completed(self):
     #   return True
        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
      #  qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
       # if qs:
        #    return False
        #else:
         #   return True

    def can_process(self, user):
        if self.referral.processing_status=='with_referral':
            group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
            #user=request.user
            if group and group[0] in user.apiaryreferralgroup_set.all():
                return True
            else:
                return False
        return False


    def recall(self,request):
        #import ipdb; ipdb.set_trace();
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.referral.processing_status = 'recalled'
            self.referral.save()
            # TODO Log proposal action
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_RECALL_REFERRAL.format(
                    self.referral.id,
                    self.referral.proposal.id
                    ),
                request
                )
            # TODO log organisation action
            applicant_field=getattr(
                    self.referral.proposal, 
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                ProposalUserAction.APIARY_RECALL_REFERRAL.format(
                    self.referral.id,
                    self.referral.proposal.id
                    ),
                request
                )

    def remind(self,request):
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_ACTION_REMIND_REFERRAL.format(
                self.referral.id,
                self.referral.proposal.id,'{}'.format(self.referral_group.name)
                ),
                request
                )
            # Create a log entry for the organisation
            applicant_field=getattr(
                    self.referral.proposal, 
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                ProposalUserAction.APIARY_ACTION_REMIND_REFERRAL.format(
                self.referral.id,
                self.referral.proposal.id,'{}'.format(self.referral_group.name)
                ),
                request
                )
            # send email
            recipients = self.referral_group.members_list
            send_apiary_referral_email_notification(self.referral,recipients,request,reminder=True)

    def resend(self,request):
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.referral.processing_status = 'with_referral'
            self.referral.proposal.processing_status = 'with_referral'
            self.referral.proposal.save()
            self.sent_from = 1
            self.save()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_ACTION_RESEND_REFERRAL_TO.format(
                    self.referral.id,
                    self.referral.proposal.id,'{}'.format(self.referral_group.name)
                    ),
                request)
            # Create a log entry for the organisation
            #self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            applicant_field=getattr(
                    self.referral.proposal, 
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                    ProposalUserAction.APIARY_ACTION_RESEND_REFERRAL_TO.format(
                        self.referral.id,
                        self.referral.proposal.id,
                        '{}'.format(
                            self.referral_group.name)
                        ),
                    request
                    )
            # send email
            recipients = self.referral_group.members_list
            send_apiary_referral_email_notification(self.referral,recipients,request)

    def complete(self,request):
        with transaction.atomic():
            try:
                #if request.user != self.referral:
                group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
                #print u.referralrecipientgroup_set.all()
                user=request.user
                if group and group[0] not in user.apiaryreferralgroup_set.all():
                    raise exceptions.ReferralNotAuthorized()
                self.referral.processing_status = 'completed'
                #self.referral.referral = request.user
                self.referral.referral_text = request.user.get_full_name() + ': ' + request.data.get('referral_comment')
                #self.add_referral_document(request)
                self.referral.save()
                # TODO Log proposal action
                #self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                self.referral.proposal.log_user_action(
                        ProposalUserAction.APIARY_CONCLUDE_REFERRAL.format(
                            request.user.get_full_name(), 
                            self.referral.id,
                            self.referral.proposal.id,
                            '{}'.format(
                                self.referral_group.name)
                            ),
                        request
                        )
                # TODO log organisation action
                #self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                #import ipdb;ipdb.set_trace();
                applicant_field=getattr(
                        self.referral.proposal, 
                        self.referral.proposal.applicant_field
                        )
                applicant_field.log_user_action(
                        ProposalUserAction.APIARY_CONCLUDE_REFERRAL.format(
                            request.user.get_full_name(), 
                            self.referral.id,
                            self.referral.proposal.id,
                            '{}'.format(self.referral_group.name)
                            ),
                        request
                        )
                send_apiary_referral_complete_email_notification(self.referral, request, request.user)
            except:
                raise

    # calling ProposalApiary send_referral instead
    def send_referral(self,request,referral_email,referral_text):
        #import ipdb; ipdb.set_trace()
        with transaction.atomic():
            try:
                if self.referral.proposal.processing_status == 'with_referral':
                    group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
                    #print u.referralrecipientgroup_set.all()
                    user=request.user
                    if group and group[0] not in user.apiaryreferralgroup_set.all():
                        raise exceptions.ReferralNotAuthorized()
                    #if request.user != self.referral.referral:
                     #   raise exceptions.ReferralNotAuthorized()
                    if self.referral.sent_from != 1:
                        raise exceptions.ReferralCanNotSend()
                    self.referral.proposal.processing_status = 'with_referral'
                    self.referral.proposal.save()
                    referral = None
                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email.lower())
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    qs=Referral.objects.filter(sent_by=user, proposal=self.referral.proposal)
                    if qs:
                        raise ValidationError('You cannot send referral to this user')
                    try:
                        Referral.objects.get(referral=user,proposal=self.referral.proposal)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral (second level)
                        referral = Referral.objects.create(
                            proposal = self.referral.proposal,
                            referral=user,
                            sent_by=request.user,
                            sent_from=2,
                            text=referral_text
                        )
                        # TODO also create ApiaryReferral

                        # try:
                        #     referral_assessment=ProposalAssessment.objects.get(proposal=self,referral_group=referral_group, referral_assessment=True, referral=referral)
                        # except ProposalAssessment.DoesNotExist:
                        #     referral_assessment=ProposalAssessment.objects.create(proposal=self,referral_group=referral_group, referral_assessment=True, referral=referral)
                        #     checklist=ChecklistQuestion.objects.filter(list_type='referral_list', obsolete=False)
                        #     for chk in checklist:
                        #         try:
                        #             chk_instance=ProposalAssessmentAnswer.objects.get(question=chk, assessment=referral_assessment)
                        #         except ProposalAssessmentAnswer.DoesNotExist:
                        #             chk_instance=ProposalAssessmentAnswer.objects.create(question=chk, assessment=referral_assessment)
                    # Create a log entry for the proposal
                    self.referral.proposal.log_user_action(
                            ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                referral.id,
                                self.referral.proposal.id,
                                '{}({})'.format(
                                    user.get_full_name(),
                                    user.email
                                    )
                                ),
                            request
                            )
                    # Create a log entry for the organisation
                    applicant_field=getattr(
                            self.referral.proposal, 
                            self.referral.proposal.applicant_field
                            )
                    applicant_field.log_user_action(
                            ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                referral.id,
                                self.referral.proposal.id,
                                '{}({})'.format(
                                    user.get_full_name(),
                                    user.email
                                    )
                                ),
                            request
                            )
                    # send email
                    recipients = self.email_group.members_list
                    send_referral_email_notification(referral,recipients,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise


    # Properties
    @property
    def region(self):
        return self.referral.proposal.region

    @property
    def activity(self):
        return self.referral.proposal.activity

    @property
    def title(self):
        return self.referral.proposal.title

    # @property
    # def applicant(self):
    #     return self.proposal.applicant.name

    @property
    def applicant(self):
        return self.referral.proposal.applicant

    @property
    def can_be_processed(self):
        return self.referral.processing_status == 'with_referral'

    def can_assess_referral(self,user):
        return self.referral.processing_status == 'with_referral'

# --------------------------------------------------------------------------------------
# Apiary Models End
# --------------------------------------------------------------------------------------


import reversion
#reversion.register(Proposal, follow=['requirements', 'documents', 'compliances', 'referrals', 'approvals', 'proposal_apiary'])
reversion.register(Proposal, follow=['proposal_apiary'])
reversion.register(ProposalType)
reversion.register(ProposalRequirement)            # related_name=requirements
reversion.register(ProposalStandardRequirement)    # related_name=proposal_requirements
reversion.register(ProposalDocument)               # related_name=documents
reversion.register(ProposalLogEntry)
reversion.register(ProposalUserAction)
reversion.register(ComplianceRequest)
reversion.register(AmendmentRequest)
reversion.register(Assessment)
reversion.register(Referral)
reversion.register(HelpPage)
reversion.register(ApplicationType)
# reversion.register(ProposalApiary, follow=['apiary_sites'])
reversion.register(ProposalApiary)
#reversion.register(ProposalApiary)
reversion.register(ApiarySite)


