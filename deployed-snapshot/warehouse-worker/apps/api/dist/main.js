"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const common_1 = require("@nestjs/common");
const core_1 = require("@nestjs/core");
const swagger_1 = require("@nestjs/swagger");
const cookieParser = require("cookie-parser");
const express_1 = require("express");
const app_module_1 = require("./app.module");
const structured_logger_service_1 = require("./observability/structured-logger.service");
async function bootstrap() {
    process.env.RUNTIME_SERVICE_NAME = 'api';
    Object.defineProperty(BigInt.prototype, 'toJSON', { value() { return this.toString(); }, configurable: true });
    const app = await core_1.NestFactory.create(app_module_1.AppModule, { bufferLogs: true });
    if (process.env.DEPLOYMENT_ENVIRONMENT?.toUpperCase() === 'PRODUCTION')
        app.getHttpAdapter().getInstance().set('trust proxy', 2);
    app.useLogger(app.get(structured_logger_service_1.StructuredLoggerService));
    app.use(cookieParser());
    app.use((0, express_1.raw)({ type: 'application/octet-stream', limit: '8mb' }));
    const corsOriginPatterns = (process.env.CORS_ORIGIN ?? 'http://localhost:5173').split(',').map((value) => value.trim()).filter(Boolean);
    app.enableCors({
        origin: (origin, callback) => callback(null, !origin || corsOriginPatterns.some((pattern) => pattern === origin || wildcardOrigin(pattern).test(origin))),
        credentials: true,
        allowedHeaders: ['Content-Type', 'Content-Range', 'X-Part-SHA256', 'X-CSRF-Token', 'X-Request-ID', 'X-Trace-ID', 'traceparent', 'Idempotency-Key', 'Last-Event-ID', 'Authorization'],
        exposedHeaders: ['Content-Range', 'X-Request-ID', 'X-Trace-ID', 'traceparent'],
    });
    app.setGlobalPrefix('api', { exclude: ['health', 'ready', 'metrics', 'version'] });
    app.useGlobalPipes(new common_1.ValidationPipe({ transform: true, whitelist: true }));
    const config = new swagger_1.DocumentBuilder()
        .setTitle('Internal Warehouse AI Platform')
        .setDescription('Authenticated warehouse, uploads, dataset versions and read-only AI queries.')
        .setVersion('0.1.0')
        .build();
    swagger_1.SwaggerModule.setup('api/docs', app, swagger_1.SwaggerModule.createDocument(app, config));
    await app.listen(Number(process.env.API_PORT ?? 3000));
}
function wildcardOrigin(pattern) { return new RegExp(`^${pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replaceAll('*', '[^/]+')}$`); }
void bootstrap();
//# sourceMappingURL=main.js.map