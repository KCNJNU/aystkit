import { canReadData } from '../auth/permissions';
import { BadRequestException, ForbiddenException, Injectable } from '@nestjs/common';
import { isUUID } from 'class-validator';
import { AuthUser } from '../auth/auth.types';
import { HealthService } from '../health/health.service';
import { AITool } from './ai-provider';
import { QueryResult, WarehouseQueryService } from '../warehouse/warehouse-query.service';

const definitions = [
  ['warehouse_list_datasets', '列出最近更新的可访问数据集', 'list'],
  ['warehouse_search_datasets', '按关键词查找可访问数据集', 'search'],
  ['warehouse_get_dataset', '查看一个可访问数据集的元信息', 'dataset'],
  ['warehouse_list_versions', '查询指定数据集的版本，最新版本优先', 'versions'],
  ['warehouse_list_recent_uploads', '最近上传的可访问文件', 'list'],
  ['warehouse_search_files', '按文件名查找可访问文件', 'search'],
  ['warehouse_get_file_metadata', '文件元信息，不读取文件内容', 'file'],
  ['warehouse_get_storage_summary', '当前用户可访问文件的存储用量', 'none'],
  ['warehouse_get_dataset_statistics', '当前用户可访问的数据集、版本、文件数量', 'none'],
  ['system_get_api_status', '读取真实 API 依赖状态，包含 Qwen', 'none'],
  ['system_get_system_status', '读取真实运行状态和版本', 'none'],
] as const;

@Injectable()
export class AIToolRegistry {
  constructor(private readonly warehouse: WarehouseQueryService, private readonly health: HealthService) {}
  readonly tools: AITool[] = definitions.map(([name, description, kind]) => ({ type: 'function', function: { name, description, parameters: {
    type: 'object', additionalProperties: false,
    properties: { ...(['list', 'search', 'versions'].includes(kind) ? { limit: { type: 'integer', minimum: 1, maximum: 20 } } : {}), ...(kind === 'search' ? { keyword: { type: 'string', minLength: 1, maxLength: 100 } } : {}), ...(['dataset', 'versions'].includes(kind) ? { dataset_id: { type: 'string', format: 'uuid' } } : {}), ...(kind === 'file' ? { file_id: { type: 'string', format: 'uuid' } } : {}) },
    required: kind === 'search' ? ['keyword'] : ['dataset', 'versions'].includes(kind) ? ['dataset_id'] : kind === 'file' ? ['file_id'] : [],
  } } }));
  async execute(name: string, raw: string, user: AuthUser): Promise<QueryResult> {
    if (name.startsWith('warehouse_') && !canReadData(user.role)) throw new ForbiddenException('当前角色仅有基础权限，不能查询文件或数据集');
    const definition = this.tools.find(tool => tool.function.name === name);
    if (!definition || raw.length > 2048) throw new BadRequestException('不允许的 AI 工具或参数过长');
    let args: Record<string, unknown>;
    try { args = JSON.parse(raw); } catch { throw new BadRequestException('工具参数必须为 JSON'); }
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new BadRequestException('工具参数必须为对象');
    const schema = definition.function.parameters;
    const allowed = Object.keys(schema.properties as object);
    if (Object.keys(args).some(key => !allowed.includes(key)) || (schema.required as string[]).some(key => args[key] === undefined)) throw new BadRequestException('工具参数不符合白名单');
    if (args.limit !== undefined && (!Number.isInteger(args.limit) || Number(args.limit) < 1 || Number(args.limit) > 20)) throw new BadRequestException('查询条数必须为 1–20');
    if (args.keyword !== undefined && (typeof args.keyword !== 'string' || !args.keyword.trim() || args.keyword.length > 100)) throw new BadRequestException('关键词无效');
    for (const key of ['dataset_id', 'file_id']) if (args[key] !== undefined && (typeof args[key] !== 'string' || !isUUID(args[key]))) throw new BadRequestException('资源 ID 无效');
    const limit = (args.limit as number | undefined) ?? 10;
    switch (name) {
      case 'warehouse_list_datasets': return this.warehouse.datasets(user, '', limit);
      case 'warehouse_search_datasets': return this.warehouse.datasets(user, args.keyword as string, limit);
      case 'warehouse_get_dataset': return this.warehouse.dataset(user, args.dataset_id as string);
      case 'warehouse_list_versions': return this.warehouse.versions(user, args.dataset_id as string, limit);
      case 'warehouse_list_recent_uploads': return this.warehouse.files(user, '', limit);
      case 'warehouse_search_files': return this.warehouse.files(user, args.keyword as string, limit);
      case 'warehouse_get_file_metadata': return this.warehouse.file(user, args.file_id as string);
      case 'warehouse_get_storage_summary': case 'warehouse_get_dataset_statistics': return this.warehouse.statistics(user);
      case 'system_get_api_status': case 'system_get_system_status': return { data: await this.health.check(), sources: [{ type: 'system', id: 'status', name: '实时系统状态' }] };
      default: throw new BadRequestException('工具未注册');
    }
  }
}

