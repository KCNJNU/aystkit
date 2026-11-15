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
exports.ConfirmFileActionDto = exports.RecycleQueryDto = exports.RelocateFileDto = void 0;
const class_transformer_1 = require("class-transformer");
const swagger_1 = require("@nestjs/swagger");
const class_validator_1 = require("class-validator");
class RelocateFileDto {
    originalName;
    folderPath;
    confirmed;
}
exports.RelocateFileDto = RelocateFileDto;
__decorate([
    (0, swagger_1.ApiProperty)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MaxLength)(200),
    __metadata("design:type", String)
], RelocateFileDto.prototype, "originalName", void 0);
__decorate([
    (0, swagger_1.ApiProperty)({ description: '数据集内已有文件夹的路径，根目录传空字符串' }),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MaxLength)(750),
    __metadata("design:type", String)
], RelocateFileDto.prototype, "folderPath", void 0);
__decorate([
    (0, swagger_1.ApiProperty)({ enum: [true] }),
    (0, class_validator_1.Equals)(true),
    __metadata("design:type", Boolean)
], RelocateFileDto.prototype, "confirmed", void 0);
class RecycleQueryDto {
    page = 1;
    pageSize = 20;
    search;
}
exports.RecycleQueryDto = RecycleQueryDto;
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    __metadata("design:type", Object)
], RecycleQueryDto.prototype, "page", void 0);
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(100),
    __metadata("design:type", Object)
], RecycleQueryDto.prototype, "pageSize", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MaxLength)(200),
    __metadata("design:type", String)
], RecycleQueryDto.prototype, "search", void 0);
class ConfirmFileActionDto {
    confirmed;
}
exports.ConfirmFileActionDto = ConfirmFileActionDto;
__decorate([
    (0, swagger_1.ApiProperty)({ enum: [true] }),
    (0, class_validator_1.Equals)(true),
    __metadata("design:type", Boolean)
], ConfirmFileActionDto.prototype, "confirmed", void 0);
//# sourceMappingURL=file-lifecycle.dto.js.map