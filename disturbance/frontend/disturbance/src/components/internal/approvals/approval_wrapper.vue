<template>
<div class="container">
    <div v-if="approvalId">
        <div v-if="apiaryApproval">
            <ApiaryApproval :approvalId="approvalId"/>
        </div>
        <div v-else>
            <Approval :approvalId="approvalId"/>
        </div>
    </div>

</div>
</template>
<script>

import ApiaryApproval from './apiary_approval.vue';
import Approval from './approval.vue';
import Vue from 'vue';
import {
  api_endpoints,
  helpers
}
from '@/utils/hooks'
export default {
    name: 'ApprovalWrapper',
    data() {
        let vm = this;
        return {
            approvalId: null,
            apiaryApproval: false,
        }
    },
    components:{
        Approval,
        ApiaryApproval,
    },
    watch: {},
    computed: {
    },
    methods: {
    },
    mounted: function () {
    },
    beforeRouteEnter: function(to, from, next) {
          Vue.http.get(`/api/approvals/${to.params.approval_id}/approval_wrapper.json`).then(res => {
              next(vm => {
                  vm.approvalId = res.body.id;
                  vm.apiaryApproval = res.body.apiary_approval;
              });
            },
            err => {
              console.log(err);
            });
    },
}
</script>
