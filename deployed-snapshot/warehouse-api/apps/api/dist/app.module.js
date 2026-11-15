"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.AppModule = void 0;
const warehouse_module_1 = require("./warehouse/warehouse.module");
const ai_module_1 = require("./ai/ai.module");
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const health_module_1 = require("./health/health.module");
const files_module_1 = require("./files/files.module");
const datasets_module_1 = require("./datasets/datasets.module");
const prisma_module_1 = require("./prisma/prisma.module");
const storage_module_1 = require("./storage/storage.module");
const search_module_1 = require("./search/search.module");
const auth_module_1 = require("./auth/auth.module");
const audit_module_1 = require("./audit/audit.module");
const jobs_module_1 = require("./jobs/jobs.module");
const notifications_module_1 = require("./notifications/notifications.module");
const settings_module_1 = require("./settings/settings.module");
const users_module_1 = require("./users/users.module");
const tags_module_1 = require("./tags/tags.module");
const saved_views_module_1 = require("./saved-views/saved-views.module");
const uploads_module_1 = require("./uploads/uploads.module");
const worker_module_1 = require("./worker/worker.module");
const activity_module_1 = require("./activity/activity.module");
const root_env_1 = require("./root-env");
const observability_module_1 = require("./observability/observability.module");
const build_info_module_1 = require("./build-info/build-info.module");
const archive_module_1 = require("./archive/archive.module");
let AppModule = class AppModule {
};
exports.AppModule = AppModule;
exports.AppModule = AppModule = __decorate([
    (0, common_1.Module)({
        imports: [
            config_1.ConfigModule.forRoot({ isGlobal: true, envFilePath: root_env_1.ROOT_ENV_FILE }),
            observability_module_1.ObservabilityModule,
            build_info_module_1.BuildInfoModule,
            archive_module_1.ArchiveModule,
            auth_module_1.AuthModule,
            audit_module_1.AuditModule,
            health_module_1.HealthModule,
            warehouse_module_1.WarehouseModule,
            ai_module_1.AIModule,
            prisma_module_1.PrismaModule,
            storage_module_1.StorageModule,
            files_module_1.FilesModule,
            datasets_module_1.DatasetsModule,
            search_module_1.SearchModule,
            jobs_module_1.JobsModule,
            notifications_module_1.NotificationsModule,
            settings_module_1.SettingsModule,
            users_module_1.UsersModule,
            tags_module_1.TagsModule,
            saved_views_module_1.SavedViewsModule,
            uploads_module_1.UploadsModule,
            worker_module_1.WorkerModule,
            activity_module_1.ActivityModule,
        ],
    })
], AppModule);
//# sourceMappingURL=app.module.js.map