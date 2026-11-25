<script setup lang="ts">
import { computed } from 'vue'
import Icon from './Icon.vue'

const props = defineProps<{ page: number; size: number; total: number }>()
const emit = defineEmits<{ 'update:page': [page: number] }>()

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.size)))
</script>

<template>
  <nav v-if="total > size" class="pager" aria-label="分页">
    <button
      class="icon-btn prev"
      type="button"
      title="上一页"
      aria-label="上一页"
      :disabled="page <= 1"
      @click="emit('update:page', page - 1)"
    >
      <Icon name="chevron" />
    </button>
    <span>第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条</span>
    <button
      class="icon-btn next"
      type="button"
      title="下一页"
      aria-label="下一页"
      :disabled="page >= pages"
      @click="emit('update:page', page + 1)"
    >
      <Icon name="chevron" />
    </button>
  </nav>
</template>
