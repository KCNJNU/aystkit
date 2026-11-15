import { Type } from 'class-transformer';
import { ApiProperty } from '@nestjs/swagger';
import { Equals, IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

export class RelocateFileDto {
  @ApiProperty() @IsString() @MaxLength(200) originalName!: string;
  @ApiProperty({ description: '数据集内已有文件夹的路径，根目录传空字符串' }) @IsString() @MaxLength(750) folderPath!: string;
  @ApiProperty({ enum: [true] }) @Equals(true) confirmed!: true;
}
export class RecycleQueryDto {
  @Type(() => Number) @IsInt() @Min(1) page = 1;
  @Type(() => Number) @IsInt() @Min(1) @Max(100) pageSize = 20;
  @IsOptional() @IsString() @MaxLength(200) search?: string;
}
export class ConfirmFileActionDto {
  @ApiProperty({ enum: [true] }) @Equals(true) confirmed!: true;
}
