from __future__ import unicode_literals

import datetime
from django.db import models,transaction
from django.dispatch import receiver
from django.db.models.signals import pre_delete
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields.jsonb import JSONField
from django.utils import timezone
from django.contrib.gis.db.models.fields import PointField
from django.contrib.gis.db.models.manager import GeoManager
from ledger.accounts.models import EmailUser, RevisionedMixin
from disturbance.components.approvals.pdf import create_approval_document
from disturbance.components.organisations.models import Organisation
from disturbance.components.proposals.models import Proposal, ProposalUserAction, ApiarySite, ApiarySiteOnProposal
from disturbance.components.main.models import CommunicationsLogEntry, UserAction, Document
from disturbance.components.approvals.email import (
    send_approval_expire_email_notification,
    send_approval_cancel_email_notification,
    send_approval_suspend_email_notification,
    send_approval_reinstate_email_notification,
    send_approval_surrender_email_notification
)
from disturbance.doctopdf import create_apiary_licence_pdf_contents
from disturbance.settings import SITE_STATUS_CURRENT, SITE_STATUS_NOT_TO_BE_REISSUED, SITE_STATUS_SUSPENDED, \
    SITE_STATUS_TRANSFERRED
from disturbance.utils import search_keys, search_multiple_keys
from disturbance.helpers import is_customer
from django_countries.fields import CountryField

import logging
logger = logging.getLogger(__name__)


def update_approval_doc_filename(instance, filename):
    return_str = ''
    if instance.approval.apiary_approval:
        return_str = 'approvals/{}/documents/{}/{}'.format(instance.approval.id, instance.id, filename)
    else:
        return_str = 'approvals/{}/documents/{}'.format(instance.approval.id,filename)
    return return_str


def update_approval_comms_log_filename(instance, filename):
    return 'approvals/{}/communications/{}/{}'.format(instance.log_entry.approval.id,instance.id,filename)


class ApprovalDocument(Document):
    approval = models.ForeignKey('Approval',related_name='documents')
    _file = models.FileField(upload_to=update_approval_doc_filename)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ApprovalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'disturbance'


class RenewalDocument(Document):
    approval = models.ForeignKey('Approval',related_name='renewal_documents')
    _file = models.FileField(upload_to=update_approval_doc_filename)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(RenewalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'disturbance'


class ApiarySiteOnApproval(models.Model):
    apiary_site = models.ForeignKey('ApiarySite',)
    approval = models.ForeignKey('Approval',)
    available = models.BooleanField(default=False)
    site_status = models.CharField(default=SITE_STATUS_CURRENT, max_length=20)
    # site_available = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    wkb_geometry = PointField(srid=4326, blank=True, null=True)  # store approved coordinates
    site_category = models.ForeignKey('SiteCategory', null=True, blank=True,)
    objects = GeoManager()

    def __str__(self):
        return 'id:{}: (apiary_site: {}, approval: {})'.format(self.id, self.apiary_site.id, self.approval.id)

    class Meta:
        app_label = 'disturbance'
        unique_together = ['apiary_site', 'approval',]


class Approval(RevisionedMixin):
    STATUS_CURRENT = 'current'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS_SURRENDERED = 'surrendered'
    STATUS_SUSPENDED = 'suspended'
    STATUS_CHOICES = (
        (STATUS_CURRENT, 'Current'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_SURRENDERED, 'Surrendered'),
        (STATUS_SUSPENDED, 'Suspended')
    )
    lodgement_number = models.CharField(max_length=9, blank=True, default='')
    status = models.CharField(max_length=40, choices=STATUS_CHOICES,
                                       default=STATUS_CHOICES[0][0])
    # NB: licence_document not used for Apiary applications
    licence_document = models.ForeignKey(ApprovalDocument, blank=True, null=True, related_name='licence_document')
    cover_letter_document = models.ForeignKey(ApprovalDocument, blank=True, null=True, related_name='cover_letter_document')
    replaced_by = models.ForeignKey('self', blank=True, null=True)
    #current_proposal = models.ForeignKey(Proposal,related_name = '+')
    current_proposal = models.ForeignKey(Proposal,related_name='approvals')
    renewal_document = models.ForeignKey(ApprovalDocument, blank=True, null=True, related_name='renewal_document')
    apiary_renewal_document = models.ForeignKey(RenewalDocument, blank=True, null=True, related_name='apiary_renewal_document')
    renewal_sent = models.BooleanField(default=False)
    issue_date = models.DateTimeField()
    original_issue_date = models.DateField(auto_now_add=True)
    start_date = models.DateField()
    expiry_date = models.DateField()
    surrender_details = JSONField(blank=True,null=True)
    suspension_details = JSONField(blank=True,null=True)
    applicant = models.ForeignKey(Organisation,on_delete=models.PROTECT, blank=True, null=True, related_name='disturbance_approvals')
    proxy_applicant = models.ForeignKey(EmailUser,on_delete=models.PROTECT, blank=True, null=True, related_name='disturbance_proxy_approvals')
    extracted_fields = JSONField(blank=True, null=True)
    cancellation_details = models.TextField(blank=True)
    cancellation_date = models.DateField(blank=True, null=True)
    set_to_cancel = models.BooleanField(default=False)
    set_to_suspend = models.BooleanField(default=False)
    set_to_surrender = models.BooleanField(default=False)
    reissued= models.BooleanField(default=False)
    apiary_approval = models.BooleanField(default=False)
    no_annual_rental_fee_until = models.DateField(blank=True, null=True)
    apiary_sites = models.ManyToManyField('ApiarySite', through=ApiarySiteOnApproval, related_name='approval_set')
    migrated = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'
        unique_together = ('lodgement_number', 'issue_date')

    def add_apiary_sites_to_proposal_apiary_for_renewal(self, proposal_apiary):
        for apiary_site in self.apiary_sites.all():  # Exclude just in case there is.
            relation = self.get_relation(apiary_site)
            ApiarySiteOnProposal.objects.create(
                apiary_site=apiary_site,
                proposal_apiary=proposal_apiary,
                wkb_geometry_draft=relation.wkb_geometry,
                site_category_draft=relation.site_category,
                for_renewal=True,
            )

    def get_relation(self, apiary_site):
        if isinstance(apiary_site, dict):
            apiary_site = ApiarySite.objects.get(id=apiary_site.get('id'))
        relation_obj = ApiarySiteOnApproval.objects.get(apiary_site=apiary_site, approval=self)
        return relation_obj

    def get_relations(self):
        # relation_objs = ApiarySiteOnApproval.objects.filter(apiary_site__in=self.apiary_sites.all(), approval=self)
        relation_objs = ApiarySiteOnApproval.objects.filter(apiary_site__in=self.apiary_sites.all(), approval=self).exclude(site_status=SITE_STATUS_TRANSFERRED)
        return relation_objs

    @property
    def relevant_renewal_document(self):
        return self.apiary_renewal_document if self.apiary_renewal_document else self.renewal_document

    @property
    def relevant_applicant_id(self):
        if self.applicant:
            #return self.org_applicant.organisation.id
            return self.applicant.id
        elif self.proxy_applicant:
            return self.proxy_applicant.id

    @property
    def relevant_applicant(self):
        if self.applicant:
            return self.applicant
        else:
            return self.proxy_applicant

    @property
    def relevant_applicant_email(self):
        if self.applicant and hasattr(self.applicant.organisation, 'email') and self.applicant.organisation.email:
            return self.applicant.organisation.email
        elif self.proxy_applicant:
            return self.proxy_applicant.email
        else:
            return self.current_proposal.submitter.email

    @property
    def relevant_applicant_name(self):
        if self.applicant:
            return self.applicant.name
        elif self.proxy_applicant:
            return self.proxy_applicant.get_full_name()
        else:
            return self.current_proposal.submitter.get_full_name()

    @property
    def relevant_applicant_address(self):
        if self.applicant:
            return self.applicant.address
        elif self.proxy_applicant:
            #return self.proxy_applicant.addresses.all().first()
            return self.proxy_applicant.residential_address
        else:
            return self.current_proposal.submitter.residential_address

    @property
    def region(self):
        try:
            return self.current_proposal.region.name
        except:
            return ''

    @property
    def district(self):
        try:
            return self.current_proposal.district.name
        except:
            return ''

    @property
    def tenure(self):
        try:
            return self.current_proposal.tenure.name
        except:
            return ''

    @property
    def activity(self):
        return self.current_proposal.activity

    @property
    def title(self):
        return self.current_proposal.title

    @property
    def next_id(self):
        #ids = map(int,[(i.lodgement_number.split('A')[1]) for i in Approval.objects.all()])
        ids = map(int,[i.split('A')[1] for i in Approval.objects.all().values_list('lodgement_number', flat=True) if i])
        return max(ids) + 1 if ids else 1

    def save(self, *args, **kwargs):
        super(Approval, self).save(*args,**kwargs)
        if self.lodgement_number == '':
            self.lodgement_number = 'A{0:06d}'.format(self.next_id)
            self.save()

    def __str__(self):
        return self.lodgement_number

    @property
    def reference(self):
        return 'A{}'.format(self.id)

    @property
    def can_reissue(self):
        return self.status == Approval.STATUS_CURRENT or self.status == Approval.STATUS_SUSPENDED

    @property
    def can_reinstate(self):
        return self.status in (Approval.STATUS_CANCELLED, Approval.STATUS_SUSPENDED, Approval.STATUS_SURRENDERED) and self.can_action

    @property
    def allowed_assessors(self):
        return self.current_proposal.compliance_assessors

    @property
    def allowed_approvers(self):
        return self.current_proposal.allowed_approvers

    @property
    def is_issued(self):
        return self.licence_number is not None and len(self.licence_number) > 0

    @property
    def can_action(self):
        if not (self.set_to_cancel or self.set_to_suspend or self.set_to_surrender):
                return True
        else:
            return False

    @property
    def can_renew(self):
        try:
            renew_conditions = {
                    'previous_application': self.current_proposal,
                    'proposal_type': 'renewal'
                    }
            proposal = Proposal.objects.get(**renew_conditions)
            if proposal:
                # Proposal for the renewal already exists.
                return False
        except Proposal.DoesNotExist:
            # Proposal for the renewal doesn't exit
            return True

    @property
    def can_amend(self):
        if not self.apiary_approval:
            try:
                amend_conditions = {
                        'previous_application': self.current_proposal,
                        'proposal_type': 'amendment'
                        }
                proposal=Proposal.objects.get(**amend_conditions)
                if proposal:
                    return False
            except Proposal.DoesNotExist:
                if self.can_renew:
                    return True
                else:
                    return False

    def generate_apiary_site_transfer_doc(self, request_user, site_transfer_proposal, preview=False):
        return self.generate_apiary_licence_doc(site_transfer_proposal, request_user, preview)
        # copied_to_permit = self.copiedToPermit_fields(site_transfer_proposal)  # Get data related to isCopiedToPermit tag
        # if preview:
        #     pdf_contents = create_apiary_licence_pdf_contents(self, site_transfer_proposal, copied_to_permit, user)
        #     return pdf_contents
        # self.licence_document = create_approval_document(self, site_transfer_proposal, copied_to_permit, user)
        # self.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))
        # self.current_proposal.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))

    def generate_doc(self, request_user, preview=False):
        #if self.current_proposal and self.current_proposal.apiary_group_application_type:
        if self.apiary_approval:
            return self.generate_apiary_licence_doc(self.current_proposal, request_user, preview)
        else:
            from disturbance.components.approvals.pdf import create_approval_doc, create_approval_pdf_bytes
            copied_to_permit = self.copiedToPermit_fields(self.current_proposal) #Get data related to isCopiedToPermit tag
            if preview:
                return create_approval_pdf_bytes(self,self.current_proposal, copied_to_permit, request_user)
            self.licence_document = create_approval_doc(self,self.current_proposal, copied_to_permit, request_user)
            self.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))
            self.current_proposal.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))

    def generate_apiary_licence_doc(self, proposal, request_user, preview=False):
        #import ipdb; ipdb.set_trace()
        copied_to_permit = self.copiedToPermit_fields(proposal) #Get data related to isCopiedToPermit tag
        if preview:
            pdf_contents = create_apiary_licence_pdf_contents(self, proposal, copied_to_permit, request_user)
            return pdf_contents
        #self.licence_document = create_apiary_licence_pdf_contents(self, proposal, copied_to_permit, request_user)
        self.licence_document = create_approval_document(self, proposal, copied_to_permit, request_user)
        self.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))
        self.current_proposal.save(version_comment='Created Approval PDF: {}'.format(self.licence_document.name))

    def generate_renewal_doc(self):
        #import ipdb; ipdb.set_trace()
        from disturbance.components.approvals.pdf import create_renewal_doc, create_apiary_renewal_doc
        if self.apiary_approval:
            self.apiary_renewal_document = create_apiary_renewal_doc(self,self.current_proposal)
            self.save(version_comment='Created Approval PDF: {}'.format(self.apiary_renewal_document.name))
            self.current_proposal.save(version_comment='Created Approval PDF: {}'.format(self.apiary_renewal_document.name))
        else:
            self.renewal_document = create_renewal_doc(self,self.current_proposal)
            self.save(version_comment='Created Approval PDF: {}'.format(self.renewal_document.name))
            self.current_proposal.save(version_comment='Created Approval PDF: {}'.format(self.renewal_document.name))

    def copiedToPermit_fields(self, proposal):
        p=proposal
        copied_data = []
        search_assessor_data = []
        search_schema = search_multiple_keys(p.schema, primary_search='isCopiedToPermit', search_list=['label', 'name'])
        if p.assessor_data:
            search_assessor_data=search_keys(p.assessor_data, search_list=['assessor', 'name'])
        if search_schema:
            for c in search_schema:
                try:
                    if search_assessor_data:
                        for d in search_assessor_data:
                            if c['name'] == d['name']:
                                if d['assessor']:
                                    #copied_data.append({c['label'], d['assessor']})
                                    copied_data.append({c['label']:d['assessor']})
                except:
                    raise
        return copied_data

    def log_user_action(self, action, request):
       return ApprovalUserAction.log_action(self, action, request.user)

    def expire_approval(self,user):
        with transaction.atomic():
            try:
                today = timezone.localtime(timezone.now()).date()
                if self.status == Approval.STATUS_CURRENT and self.expiry_date < today:
                    self.status = Approval.STATUS_EXPIRED
                    self.save()
                    send_approval_expire_email_notification(self)

                    # Change the statuses of the apiary sites, too
                    self.change_apiary_site_status(self.status)

                    proposal = self.current_proposal
                    ApprovalUserAction.log_action(self,ApprovalUserAction.ACTION_EXPIRE_APPROVAL.format(self.id),user)
                    ProposalUserAction.log_action(proposal,ProposalUserAction.ACTION_EXPIRED_APPROVAL_.format(proposal.id),user)
            except:
                raise

    def change_apiary_site_status(self, approval_status):
        relations = self.get_relations()
        if approval_status in (Approval.STATUS_CANCELLED, Approval.STATUS_SUSPENDED, Approval.STATUS_SURRENDERED,):
            relations.update(site_status=SITE_STATUS_NOT_TO_BE_REISSUED)
        elif approval_status == Approval.STATUS_EXPIRED:
            for apiary_site in self.apiary_sites.all():
                apiary_site.make_vacant(True, apiary_site.latest_approval_link)
            relations.update(available=False)
        elif approval_status == Approval.STATUS_CURRENT:
            relations.update(site_status=SITE_STATUS_CURRENT)

    def approval_cancellation(self,request,details):
        with transaction.atomic():
            try:
                if not request.user in self.allowed_assessors:
                    raise ValidationError('You do not have access to cancel this approval')
                if not self.can_reissue and self.can_action:
                    raise ValidationError('You cannot cancel approval if it is not current or suspended')
                self.cancellation_date = details.get('cancellation_date').strftime('%Y-%m-%d')
                self.cancellation_details = details.get('cancellation_details')
                cancellation_date = datetime.datetime.strptime(self.cancellation_date,'%Y-%m-%d')
                cancellation_date = cancellation_date.date()
                self.cancellation_date = cancellation_date
                today = timezone.now().date()
                if cancellation_date <= today:
                    if not self.status == Approval.STATUS_CANCELLED:
                        self.status = Approval.STATUS_CANCELLED
                        self.set_to_cancel = False
                        send_approval_cancel_email_notification(self)

                        # Change the statuses of the apiary sites, too
                        self.change_apiary_site_status(self.status)
                else:
                    self.set_to_cancel = True
                    send_approval_cancel_email_notification(self, future_cancel=True)
                #import ipdb; ipdb.set_trace()
                self.save()
                # Log proposal action
                self.log_user_action(ApprovalUserAction.ACTION_CANCEL_APPROVAL.format(self.id),request)
                # Log entry for organisation
                #self.current_proposal.log_user_action(ProposalUserAction.ACTION_CANCEL_APPROVAL.format(self.current_proposal.id),request)
            except:
                raise

    def approval_suspension(self,request,details):
        with transaction.atomic():
            try:
                if not request.user in self.allowed_assessors:
                    raise ValidationError('You do not have access to suspend this approval')
                if not self.can_reissue and self.can_action:
                    raise ValidationError('You cannot suspend approval if it is not current or suspended')
                if details.get('to_date'):
                    to_date= details.get('to_date').strftime('%d/%m/%Y')
                else:
                    to_date=''
                self.suspension_details = {
                    'from_date' : details.get('from_date').strftime('%d/%m/%Y'),
                    'to_date' : to_date,
                    'details': details.get('suspension_details'),
                }
                today = timezone.now().date()
                from_date = datetime.datetime.strptime(self.suspension_details['from_date'],'%d/%m/%Y')
                from_date = from_date.date()
                if from_date <= today:
                    if not self.status == Approval.STATUS_SUSPENDED:
                        self.status = Approval.STATUS_SUSPENDED
                        self.set_to_suspend = False
                        self.save()
                        send_approval_suspend_email_notification(self)

                        # Change the statuses of the apiary sites, too
                        self.change_apiary_site_status(self.status)
                else:
                    self.set_to_suspend = True
                    send_approval_suspend_email_notification(self, future_suspend=True)
                self.save()
                # Log approval action
                self.log_user_action(ApprovalUserAction.ACTION_SUSPEND_APPROVAL.format(self.id),request)
                # Log entry for proposal
                #self.current_proposal.log_user_action(ProposalUserAction.ACTION_SUSPEND_APPROVAL.format(self.current_proposal.id),request)
            except:
                raise

    def reinstate_approval(self,request):
        with transaction.atomic():
            try:
                if not request.user in self.allowed_assessors:
                    raise ValidationError('You do not have access to reinstate this approval')
                if not self.can_reinstate:
                #if not self.status == 'suspended':
                    raise ValidationError('You cannot reinstate approval at this stage')
                today = timezone.now().date()
                if not self.can_reinstate and self.expiry_date>= today:
                #if not self.status == 'suspended' and self.expiry_date >= today:
                    raise ValidationError('You cannot reinstate approval at this stage')
                if self.status == Approval.STATUS_CANCELLED:
                    self.cancellation_details =  ''
                    self.cancellation_date = None
                if self.status == Approval.STATUS_SURRENDERED:
                    self.surrender_details = {}
                if self.status == Approval.STATUS_SUSPENDED:
                    self.suspension_details = {}

                self.status = Approval.STATUS_CURRENT
                #self.suspension_details = {}
                self.save()
                send_approval_reinstate_email_notification(self, request)

                # Change the statuses of the apiary sites, too
                self.change_apiary_site_status(self.status)

                # Log approval action
                self.log_user_action(ApprovalUserAction.ACTION_REINSTATE_APPROVAL.format(self.id),request)
                # Log entry for proposal
                #self.current_proposal.log_user_action(ProposalUserAction.ACTION_REINSTATE_APPROVAL.format(self.current_proposal.id),request)
            except:
                raise

    def approval_surrender(self,request,details):
        with transaction.atomic():
            try:
                if self.applicant and not request.user.disturbance_organisations.filter(organisation_id = self.relevant_applicant_id):
                    #if not request.user in self.allowed_assessors:
                    if request.user not in self.allowed_assessors and not is_customer(request):
                        raise ValidationError('You do not have access to surrender this approval')
                if not self.can_reissue and self.can_action:
                    raise ValidationError('You cannot surrender approval if it is not current or suspended')
                self.surrender_details = {
                    'surrender_date' : details.get('surrender_date').strftime('%d/%m/%Y'),
                    'details': details.get('surrender_details'),
                }
                today = timezone.now().date()
                surrender_date = datetime.datetime.strptime(self.surrender_details['surrender_date'],'%d/%m/%Y')
                surrender_date = surrender_date.date()
                if surrender_date <= today:
                    if not self.status == Approval.STATUS_SURRENDERED:
                        self.status = Approval.STATUS_SURRENDERED
                        self.set_to_surrender = False
                        self.save()
                        send_approval_surrender_email_notification(self)

                        # Change the statuses of the apiary sites, too
                        self.change_apiary_site_status(self.status)
                else:
                    self.set_to_surrender = True
                    send_approval_surrender_email_notification(self, future_surrender=True)
                self.save()
                # Log approval action
                self.log_user_action(ApprovalUserAction.ACTION_SURRENDER_APPROVAL.format(self.id),request)
                # Log entry for proposal
                #self.current_proposal.log_user_action(ProposalUserAction.ACTION_SURRENDER_APPROVAL.format(self.current_proposal.id),request)
            except:
                raise

    def pdf_view_log(self,request):
        self.log_user_action(ApprovalUserAction.ACTION_APPROVAL_PDF_VIEW.format(self.id),request)
        return self

class PreviewTempApproval(Approval):
    class Meta:
        app_label = 'disturbance'
        #unique_together= ('lodgement_number', 'issue_date')

class ApprovalLogEntry(CommunicationsLogEntry):
    approval = models.ForeignKey(Approval, related_name='comms_logs')

    class Meta:
        app_label = 'disturbance'

    def save(self, **kwargs):
        # save the application reference if the reference not provided
        if not self.reference:
            self.reference = self.approval.id
        super(ApprovalLogEntry, self).save(**kwargs)

class ApprovalLogDocument(Document):
    log_entry = models.ForeignKey('ApprovalLogEntry',related_name='documents', null=True,)
    #approval = models.ForeignKey(Approval, related_name='comms_logs1')
    _file = models.FileField(upload_to=update_approval_comms_log_filename, null=True)
    #_file = models.FileField(upload_to=update_approval_doc_filename)

    class Meta:
        app_label = 'disturbance'

class ApprovalUserAction(UserAction):
    ACTION_CREATE_APPROVAL = "Create approval {}"
    ACTION_UPDATE_APPROVAL = "Create approval {}"
    ACTION_EXPIRE_APPROVAL = "Expire approval {}"
    ACTION_CANCEL_APPROVAL = "Cancel approval {}"
    ACTION_SUSPEND_APPROVAL = "Suspend approval {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate approval {}"
    ACTION_SURRENDER_APPROVAL = "Surrender approval {}"
    ACTION_RENEW_APPROVAL = "Create renewal Proposal for approval {}"
    ACTION_AMEND_APPROVAL = "Create amendment Proposal for approval {}"
    ACTION_APPROVAL_PDF_VIEW ="View approval PDF for approval {}"
    ACTION_UPDATE_NO_CHARGE_DATE_UNTIL = "'Do not charge annual rental fee until' date updated to {} for approval {}"

    class Meta:
        app_label = 'disturbance'
        ordering = ('-when',)

    @classmethod
    def log_action(cls, approval, action, user):
        return cls.objects.create(
            approval=approval,
            who=user,
            what=str(action)
        )

    approval= models.ForeignKey(Approval, related_name='action_logs')

class MigratedApiaryLicence(models.Model):
# Records imported from CSV
    LICENCEE_TYPE_ORGANISATION = 'organisation'
    LICENCEE_TYPE_INDIVIDUAL = 'individual'
    LICENCEE_TYPE_CHOICES = (
        (LICENCEE_TYPE_ORGANISATION, 'Organisation'),
        (LICENCEE_TYPE_INDIVIDUAL, 'Individual'),
    )

    permit_number = models.IntegerField(unique=True)
    start_date = models.DateField()
    expiry_date = models.DateField()
    issue_date = models.DateField()
    status = models.CharField(max_length=40, choices=Approval.STATUS_CHOICES,
                                       default=Approval.STATUS_CHOICES[0][0])
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    trading_name = models.CharField(max_length=256, null=True, blank=True)
    licencee = models.CharField(max_length=256, null=True, blank=True)
    abn = models.CharField(max_length=50, null=True, blank=True, verbose_name='ABN')
    first_name = models.CharField(max_length=128, blank=False, verbose_name='Given name(s)')
    last_name = models.CharField(max_length=128, blank=False)
    #data.update({'other_contact': row[12].strip()})
    address_line1 = models.CharField('Line 1', max_length=255)
    address_line2 = models.CharField('Line 2', max_length=255, blank=True)
    address_line3 = models.CharField('Line 3', max_length=255, blank=True)
    #locality = models.CharField('Suburb / Town', max_length=255)
    suburb = models.CharField('Suburb / Town', max_length=255)
    state = models.CharField(max_length=255, default='WA', blank=True)
    country = CountryField(default='AU')
    postcode = models.CharField(max_length=10)
    phone_number1 = models.CharField(max_length=50, null=True, blank=True,
                                    verbose_name="phone number", help_text='')
    phone_number2 = models.CharField(max_length=50, null=True, blank=True,
                                    verbose_name="phone number", help_text='')
    mobile_number = models.CharField(max_length=50, null=True, blank=True,
                                    verbose_name="mobile number", help_text='')
    email = models.EmailField(blank=True, null=True,)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    migrated = models.BooleanField(default=False)
    licencee_type = models.CharField(max_length=40, choices=LICENCEE_TYPE_CHOICES)

    class Meta:
        app_label = 'disturbance'
        #ordering = ('-when',)


@receiver(pre_delete, sender=Approval)
def delete_documents(sender, instance, *args, **kwargs):
    for document in instance.documents.all():
        try:
            document.delete()
        except:
            pass

import reversion
reversion.register(Approval, follow=['documents', 'approval_set', 'action_logs'])
reversion.register(ApprovalDocument)
reversion.register(ApprovalLogDocument, follow=['documents'])
reversion.register(ApprovalLogEntry)
reversion.register(ApprovalUserAction)

