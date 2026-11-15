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
var __param = (this && this.__param) || function (paramIndex, decorator) {
    return function (target, key) { decorator(target, key, paramIndex); }
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.AIController = exports.ChatDto = void 0;
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const ai_provider_1 = require("./ai-provider");
const ai_chat_service_1 = require("./ai-chat.service");
class ChatDto {
    message;
    conversation_id;
    temperature;
    top_p;
    max_tokens;
}
exports.ChatDto = ChatDto;
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    (0, class_validator_1.Matches)(/\S/),
    (0, class_validator_1.MaxLength)(4000),
    __metadata("design:type", String)
], ChatDto.prototype, "message", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], ChatDto.prototype, "conversation_id", void 0);
__decorate([
    (0, class_validator_1.ValidateIf)((_object, value) => value !== undefined),
    (0, class_validator_1.IsNumber)({}, { message: '回答随机性必须为数字' }),
    (0, class_validator_1.Min)(0),
    (0, class_validator_1.Max)(1.9),
    __metadata("design:type", Number)
], ChatDto.prototype, "temperature", void 0);
__decorate([
    (0, class_validator_1.ValidateIf)((_object, value) => value !== undefined),
    (0, class_validator_1.IsNumber)({}, { message: '候选采样范围必须为数字' }),
    (0, class_validator_1.Min)(0.01),
    (0, class_validator_1.Max)(1),
    __metadata("design:type", Number)
], ChatDto.prototype, "top_p", void 0);
__decorate([
    (0, class_validator_1.ValidateIf)((_object, value) => value !== undefined),
    (0, class_validator_1.IsInt)({ message: '最大输出长度必须为整数' }),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(8192),
    __metadata("design:type", Number)
], ChatDto.prototype, "max_tokens", void 0);
let AIController = class AIController {
    provider;
    chatService;
    constructor(provider, chatService) {
        this.provider = provider;
        this.chatService = chatService;
    }
    async status() { return { ...await this.provider.healthCheck(), generation: this.provider.generationSettings }; }
    list(user) { return this.chatService.list(user); }
    history(user, id) { return this.chatService.history(user, id); }
    clear(user, id) { return this.chatService.clear(user, id); }
    chat(user, body) { return this.chatService.chat(user, body.message.trim(), body.conversation_id, undefined, undefined, body); }
    async stream(user, body, response) {
        const abort = new AbortController();
        const close = () => abort.abort();
        response.on('close', close);
        response.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
        response.setHeader('Cache-Control', 'no-cache, no-transform');
        response.setHeader('X-Accel-Buffering', 'no');
        response.flushHeaders();
        const heartbeat = setInterval(() => { if (!response.destroyed)
            response.write(': heartbeat\n\n'); }, 15000);
        try {
            await this.chatService.chat(user, body.message.trim(), body.conversation_id, event => { if (!response.destroyed)
                response.write(`data: ${JSON.stringify(event)}\n\n`); }, abort.signal, body);
        }
        catch (error) {
            if (!response.destroyed)
                response.write(`data: ${JSON.stringify({ type: 'error', data: { message: error instanceof common_1.HttpException ? error.message : 'AI 服务暂时不可用', status: error instanceof common_1.HttpException ? error.getStatus() : 500 } })}\n\n`);
        }
        finally {
            clearInterval(heartbeat);
            response.off('close', close);
            response.end();
        }
    }
};
exports.AIController = AIController;
__decorate([
    (0, common_1.Get)('status'),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", []),
    __metadata("design:returntype", Promise)
], AIController.prototype, "status", null);
__decorate([
    (0, common_1.Get)('conversations'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], AIController.prototype, "list", null);
__decorate([
    (0, common_1.Get)('conversations/:id'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Param)('id', common_1.ParseUUIDPipe)),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, String]),
    __metadata("design:returntype", void 0)
], AIController.prototype, "history", null);
__decorate([
    (0, common_1.Delete)('conversations/:id/messages'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Param)('id', common_1.ParseUUIDPipe)),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, String]),
    __metadata("design:returntype", void 0)
], AIController.prototype, "clear", null);
__decorate([
    (0, common_1.Post)('chat'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, ChatDto]),
    __metadata("design:returntype", void 0)
], AIController.prototype, "chat", null);
__decorate([
    (0, common_1.Post)('chat/stream'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, common_1.Res)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, ChatDto, Object]),
    __metadata("design:returntype", Promise)
], AIController.prototype, "stream", null);
exports.AIController = AIController = __decorate([
    (0, swagger_1.ApiTags)('ai'),
    (0, common_1.Controller)('ai'),
    __metadata("design:paramtypes", [ai_provider_1.AIProvider, ai_chat_service_1.AIChatService])
], AIController);
//# sourceMappingURL=ai.controller.js.map