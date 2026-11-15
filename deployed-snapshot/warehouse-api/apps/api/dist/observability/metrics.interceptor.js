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
exports.MetricsInterceptor = void 0;
const common_1 = require("@nestjs/common");
const rxjs_1 = require("rxjs");
const metrics_service_1 = require("./metrics.service");
const structured_logger_service_1 = require("./structured-logger.service");
const trace_context_service_1 = require("./trace-context.service");
let MetricsInterceptor = class MetricsInterceptor {
    metrics;
    trace;
    logger;
    constructor(metrics, trace, logger) {
        this.metrics = metrics;
        this.trace = trace;
        this.logger = logger;
    }
    intercept(context, next) {
        if (context.getType() !== 'http')
            return next.handle();
        const request = context.switchToHttp().getRequest();
        const response = context.switchToHttp().getResponse();
        const started = process.hrtime.bigint();
        if (request.user)
            this.trace.update({ userId: request.user.id });
        return next.handle().pipe((0, rxjs_1.finalize)(() => {
            const seconds = Number(process.hrtime.bigint() - started) / 1_000_000_000;
            const route = routeName(request);
            this.metrics.observeHttp(request.method, route, response.statusCode, seconds);
            this.logger.event(response.statusCode >= 500 ? 'error' : 'log', 'http_request_completed', { method: request.method, route, statusCode: response.statusCode, durationMs: Math.round(seconds * 1000) });
        }));
    }
};
exports.MetricsInterceptor = MetricsInterceptor;
exports.MetricsInterceptor = MetricsInterceptor = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [metrics_service_1.MetricsService, trace_context_service_1.TraceContextService, structured_logger_service_1.StructuredLoggerService])
], MetricsInterceptor);
function routeName(request) {
    const route = request.route?.path;
    return route ? `${request.baseUrl ?? ''}${route}` : 'unmatched';
}
//# sourceMappingURL=metrics.interceptor.js.map