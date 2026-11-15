import { ApiProperty } from '@nestjs/swagger';
import { IsNotEmpty, IsString } from 'class-validator';
export class DeleteDatasetDto {
  @ApiProperty({ description: '输入完整数据集名称以确认删除' })
  @IsString() @IsNotEmpty() confirmationName!: string;
}
