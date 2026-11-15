"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const core_1 = require("@nestjs/core");
const app_module_1 = require("./app.module");
const worker_runner_service_1 = require("./worker/worker-runner.service");
const structured_logger_service_1 = require("./observability/structured-logger.service");
async function bootstrap() {
    process.env.RUNTIME_SERVICE_NAME = 'worker';
    const context = await core_1.NestFactory.createApplicationContext(app_module_1.AppModule, { bufferLogs: true });
    context.useLogger(context.get(structured_logger_service_1.StructuredLoggerService));
    try {
        await context.get(worker_runner_service_1.WorkerRunnerService).start();
    }
    finally {
        await context.close();
    }
}
void bootstrap();
//# sourceMappingURL=worker.js.map