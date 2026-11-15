export const statusNames: Record<string, string> = { QUEUED: '排队中', RUNNING: '运行中', SUCCEEDED: '已成功', FAILED: '已失败', CANCELLED: '已取消', OPEN: '待处理', IGNORED: '已忽略', RESOLVED: '已解决', DRAFT: '草稿', FROZEN: '已冻结', PUBLISHED: '已发布', ARCHIVED: '已归档', ORPHAN_OBJECT: '孤立对象', MISSING_OBJECT: '对象缺失', SIZE_MISMATCH: '大小不一致', ERROR: '错误', WARNING: '警告', INFO: '提示' };
export const jobNames: Record<string, string> = { DATASET_DELETE: '删除数据集', DATASET_PUBLISH: '发布数据集', RECOVERY_VERIFY: '恢复校验', STORAGE_SCAN: '存储一致性扫描', STORAGE_ORPHAN_DELETE: '删除孤立对象', UPLOAD_FINALIZE: '文件上传归档', RETENTION_CLEANUP: '到期数据清理', ARCHIVE_VIDEO_ASSEMBLE: '生成完整录像' };
export const resourceNames: Record<string, string> = { Dataset: '数据集', DatasetVersion: '数据集版本', RobotFile: '文件', UploadSession: '上传任务', ArchiveRecording: '相机录像', User: '用户', Job: '后台任务', StorageReconciliationRun: '存储扫描', Robot: '数据来源' };
export const stepNames: Record<string, string> = { 'Delete Dataset Objects': '清理数据集存储对象', Validation: '校验', 'Workspace Snapshot': '生成工作区快照', 'DVC Add': '登记数据快照', 'DVC Push': '推送数据快照', 'Git Commit/Tag': '保存提交与版本标签', 'Recovery Check': '恢复检查', 'Persist Version': '保存版本', 'Prepare Temporary Workspace': '准备临时工作区', 'Checkout Git Tag': '切换版本标签', 'DVC Pull': '拉取数据快照', 'Verify Manifest and Files': '校验清单与文件', Cleanup: '清理临时数据', 'Load Database Index': '读取数据库索引', 'List ObjectStorage': '读取存储对象列表', 'Compare Objects': '核对存储对象', 'Persist Issues': '保存检查问题', 'Validate Issues': '确认问题状态', 'Delete Objects': '删除存储对象', 'Resolve Issues': '更新处理结果', 'Validate Parts': '校验上传分片', 'Compose Object': '合并文件分片', 'Verify Format and Checksum': '校验格式与校验和', 'Create RobotFile': '创建文件记录', 'Cleanup Parts': '清理分片', 'Validate Recovery Gates': '检查恢复条件', 'Delete Expired Data': '删除到期数据', 'Persist Audit Evidence': '保存审计记录', 'Assemble Continuous Recording': '生成并校验完整录像' };

const errors: Record<string, string> = {
  'Invalid account or password': '账号或密码不正确', 'Invalid email or password': '邮箱或密码不正确',
  'Account is temporarily locked': '账号暂时锁定，请稍后再试', 'Authentication required': '请先登录',
  'Session is invalid or expired': '登录已过期，请重新登录', 'Insufficient role': '当前角色没有此操作权限',
  'Invalid CSRF token': '请求验证已失效，请刷新页面后重试', PASSWORD_CHANGE_REQUIRED: '请先修改初始密码',
  'Current password is incorrect': '当前密码不正确', 'Password must contain at least 12 characters': '密码至少需要 12 个字符',
  'Robot file is referenced by a dataset version and cannot be deleted': '文件仍被数据集版本引用，请先移除引用后再删除',
  'Stored object not found': '存储文件不存在', 'Robot file not found': '文件不存在或无权访问',
  'A file with this checksum already exists': '相同内容的文件已经存在',
  'Dataset not found': '数据集不存在或无权访问', 'Job not found': '任务不存在或无权访问',
  'Only FAILED jobs can be retried': '仅失败的任务可以重试', 'Job cannot be cancelled': '当前任务状态不允许取消',
  'Upload session belongs to another user': '无权操作其他用户的上传任务',
};
export function errorText(message: string, status?: number): string {
  if (errors[message]) return errors[message];
  if (/[\u4e00-\u9fff]/.test(message)) return message;
  if (/must be|must contain|should not|is required/i.test(message)) return '输入格式不正确，请检查必填项、邮箱、密码长度和选择的数据范围。';
  const defaults: Record<number, string> = { 400: '请求参数不正确，请检查输入内容', 401: '身份验证失败，请重新登录', 403: '无权访问该资源或执行此操作', 404: '资源不存在或无权访问', 409: '当前数据状态不允许此操作，请刷新后重试', 413: '文件超过允许的大小', 429: '操作过于频繁，请稍后重试', 500: '服务处理失败，请稍后重试或联系管理员', 502: '后端服务暂时不可用', 503: '服务暂时不可用，请查看系统状态' };
  return status && defaults[status] ? defaults[status] : message;
}

export const validationMessages: Record<string, string> = {
  EMPTY_VERSION: '版本至少需要一个文件', INVALID_CHECKSUM: '文件校验和不是有效的 SHA-256 值', INVALID_SIZE: '文件大小必须大于零',
  OBJECT_MISSING: '存储对象不存在或无法访问', SIZE_MISMATCH: '实际文件大小与数据库记录不一致', CHECKSUM_MISMATCH: '文件内容校验和与数据库记录不一致',
  OBJECT_READ_FAILED: '读取存储文件失败，请检查存储服务', OBJECT_CHECK_FAILED: '检查存储文件失败，请检查存储服务',
  MANIFEST_MISSING: '冻结或发布版本缺少文件清单引用', MANIFEST_CHECK_FAILED: '检查文件清单失败', MANIFEST_OBJECT_MISSING: '文件清单对象无法访问',
  MANIFEST_CHECKSUM_MISMATCH: '清单校验和与冻结版本不一致', MANIFEST_MEMBERSHIP_MISMATCH: '清单文件列表与数据库版本不一致',
  VERSION_AGGREGATE_MISMATCH: '版本文件数量或总大小与实际列表不一致', MANIFEST_READ_FAILED: '读取文件清单失败',
};
