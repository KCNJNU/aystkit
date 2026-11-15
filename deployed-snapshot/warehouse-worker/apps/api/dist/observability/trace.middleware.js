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
exports.TraceMiddleware = void 0;
const common_1 = require("@nestjs/common");
const node_crypto_1 = require("node:crypto");
const trace_context_service_1 = require("./trace-context.service");
let TraceMiddleware = class TraceMiddleware {
    context;
    constructor(context) {
        this.context = context;
    }
    use(request, response, next) {
        const requestId = header(request.header('x-request-id')) ?? (0, node_crypto_1.randomUUID)();
        const traceId = traceFromParent(request.header('traceparent')) ?? traceHeader(request.header('x-trace-id')) ?? (0, node_crypto_1.randomBytes)(16).toString('hex');
        request.requestId = requestId;
        request.traceId = traceId;
        request.headers['x-request-id'] = requestId;
        request.headers['x-trace-id'] = traceId;
        response.setHeader('x-request-id', requestId);
        response.setHeader('x-trace-id', traceId);
        this.context.run({ requestId, traceId }, next);
    }
};
exports.TraceMiddleware = TraceMiddleware;
exports.TraceMiddleware = TraceMiddleware = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [trace_context_service_1.TraceContextService])
], TraceMiddleware);
function header(value) { return value && value.length <= 128 ? value : undefined; }
function traceHeader(value) { return value && /^[0-9a-f]{32}$/i.test(value) ? value.toLowerCase() : undefined; }
function traceFromParent(value) { const match = /^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$/i.exec(value ?? ''); return match?.[1].toLowerCase(); }
//# sourceMappingURL=trace.middleware.js.map