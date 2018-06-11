import traceback
from django.conf import settings
from rest_framework import viewsets, serializers, status, generics, views
from rest_framework.decorators import detail_route, list_route, renderer_classes, parser_classes
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser, BasePermission
from rest_framework.pagination import PageNumberPagination
from django.urls import reverse
from disturbance.components.main.models import Region, District, Tenure, ApplicationType, ActivityMatrix
from disturbance.components.main.serializers import RegionSerializer, DistrictSerializer, TenureSerializer, ApplicationTypeSerializer, ActivityMatrixSerializer


class DistrictViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = District.objects.all().order_by('id')
    serializer_class = DistrictSerializer

class RegionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Region.objects.all().order_by('id')
    serializer_class = RegionSerializer

class ActivityMatrixViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ActivityMatrix.objects.all().order_by('id')
    serializer_class = ActivityMatrixSerializer

#class ActivityViewSet(viewsets.ReadOnlyModelViewSet):
#    queryset = Activity.objects.all().order_by('order')
#    serializer_class = ActivitySerializer

class TenureViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Tenure.objects.all().order_by('order')
    serializer_class = TenureSerializer

class ApplicationTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ApplicationType.objects.all().order_by('order')
    serializer_class = ApplicationTypeSerializer

