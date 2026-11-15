"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.AIToolRegistry = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const class_validator_1 = require("class-validator");
const health_service_1 = require("../health/health.service");
const warehouse_query_service_1 = require("../warehouse/warehouse-query.service");
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
];
let AIToolRegistry = class AIToolRegistry {
    warehouse;
    health;
    constructor(warehouse, health) {
        this.warehouse = warehouse;
        this.health = health;
    }
    tools = definitions.map(([name, description, kind]) => ({ type: 'function', function: { name, description, parameters: {
                type: 'object', additionalProperties: false,
                properties: { ...(['list', 'search', 'versions'].includes(kind) ? { limit: { type: 'integer', minimum: 1, maximum: 20 } } : {}), ...(kind === 'search' ? { keyword: { type: 'string', minLength: 1, maxLength: 100 } } : {}), ...(['dataset', 'versions'].includes(kind) ? { dataset_id: { type: 'string', format: 'uuid' } } : {}), ...(kind === 'file' ? { file_id: { type: 'string', format: 'uuid' } } : {}) },
                required: kind === 'search' ? ['keyword'] : ['dataset', 'versions'].includes(kind) ? ['dataset_id'] : kind === 'file' ? ['file_id'] : [],
            } } }));
    async execute(name, raw, user) {
        if (name.startsWith('warehouse_') && !(0, permissions_1.canReadData)(user.role))
            throw new common_1.ForbiddenException('当前角色仅有基础权限，不能查询文件或数据集');
        const definition = this.tools.find(tool => tool.function.name === name);
        if (!definition || raw.length > 2048)
            throw new common_1.BadRequestException('不允许的 AI 工具或参数过长');
        let args;
        try {
            args = JSON.parse(raw);
        }
        catch {
            throw new common_1.BadRequestException('工具参数必须为 JSON');
        }
        if (!args || typeof args !== 'object' || Array.isArray(args))
            throw new common_1.BadRequestException('工具参数必须为对象');
        const schema = definition.function.parameters;
        const allowed = Object.keys(schema.properties);
        if (Object.keys(args).some(key => !allowed.includes(key)) || schema.required.some(key => args[key] === undefined))
            throw new common_1.BadRequestException('工具参数不符合白名单');
        if (args.limit !== undefined && (!Number.isInteger(args.limit) || Number(args.limit) < 1 || Number(args.limit) > 20))
            throw new common_1.BadRequestException('查询条数必须为 1–20');
        if (args.keyword !== undefined && (typeof args.keyword !== 'string' || !args.keyword.trim() || args.keyword.length > 100))
            throw new common_1.BadRequestException('关键词无效');
        for (const key of ['dataset_id', 'file_id'])
            if (args[key] !== undefined && (typeof args[key] !== 'string' || !(0, class_validator_1.isUUID)(args[key])))
                throw new common_1.BadRequestException('资源 ID 无效');
        const limit = args.limit ?? 10;
        switch (name) {
            case 'warehouse_list_datasets': return this.warehouse.datasets(user, '', limit);
            case 'warehouse_search_datasets': return this.warehouse.datasets(user, args.keyword, limit);
            case 'warehouse_get_dataset': return this.warehouse.dataset(user, args.dataset_id);
            case 'warehouse_list_versions': return this.warehouse.versions(user, args.dataset_id, limit);
            case 'warehouse_list_recent_uploads': return this.warehouse.files(user, '', limit);
            case 'warehouse_search_files': return this.warehouse.files(user, args.keyword, limit);
            case 'warehouse_get_file_metadata': return this.warehouse.file(user, args.file_id);
            case 'warehouse_get_storage_summary':
            case 'warehouse_get_dataset_statistics': return this.warehouse.statistics(user);
            case 'system_get_api_status':
            case 'system_get_system_status': return { data: await this.health.check(), sources: [{ type: 'system', id: 'status', name: '实时系统状态' }] };
            default: throw new common_1.BadRequestException('工具未注册');
        }
    }
};
exports.AIToolRegistry = AIToolRegistry;
exports.AIToolRegistry = AIToolRegistry = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [warehouse_query_service_1.WarehouseQueryService, health_service_1.HealthService])
], AIToolRegistry);
//# sourceMappingURL=ai-tool-registry.js.map