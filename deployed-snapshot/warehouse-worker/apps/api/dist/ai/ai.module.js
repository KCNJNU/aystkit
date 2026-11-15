"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.AIModule = void 0;
const common_1 = require("@nestjs/common");
const health_module_1 = require("../health/health.module");
const prisma_module_1 = require("../prisma/prisma.module");
const redis_throttler_storage_1 = require("../auth/redis-throttler.storage");
const provider_module_1 = require("./provider.module");
const ai_controller_1 = require("./ai.controller");
const ai_chat_service_1 = require("./ai-chat.service");
const ai_tool_registry_1 = require("./ai-tool-registry");
const warehouse_module_1 = require("../warehouse/warehouse.module");
let AIModule = class AIModule {
};
exports.AIModule = AIModule;
exports.AIModule = AIModule = __decorate([
    (0, common_1.Module)({ imports: [prisma_module_1.PrismaModule, provider_module_1.ProviderModule, health_module_1.HealthModule, warehouse_module_1.WarehouseModule], controllers: [ai_controller_1.AIController], providers: [ai_chat_service_1.AIChatService, ai_tool_registry_1.AIToolRegistry, redis_throttler_storage_1.RedisThrottlerStorage] })
], AIModule);
//# sourceMappingURL=ai.module.js.map