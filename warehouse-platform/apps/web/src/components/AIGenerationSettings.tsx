import { QuestionCircleOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, InputNumber, Select, Slider, Tooltip } from 'antd';
import { type GenerationOptions } from '../services/warehouse';

export function AIGenerationSettings({ model, value, limit, disabled, onChange, onReset }: {
  model?: string; value: GenerationOptions; limit: number; disabled: boolean;
  onChange: (value: GenerationOptions) => void; onReset: () => void;
}) {
  const parameters = [
    { key: 'temperature' as const, label: '回答随机性', name: 'Temperature', min: 0, max: 1.9, step: .1, tip: '较低时回答更稳定，较高时表达更多样。数仓查询建议使用较低值。' },
    { key: 'max_tokens' as const, label: '最大输出长度', name: 'Max tokens', min: 1, max: limit, step: 1, tip: '每次模型响应允许的最大词元数量。较大时可生成更长的回答，实际用量取决于问题。' },
    { key: 'top_p' as const, label: '候选采样范围', name: 'Top p', min: .01, max: 1, step: .01, tip: '较低时优先选择高概率词语，较高时允许更多候选。通常保持默认，先调整回答随机性。' },
  ];
  return <>
    <div className="playground-settings-section"><h3>模型配置</h3><label htmlFor="ai-model-selection">当前模型</label>
      <Select id="ai-model-selection" aria-label="当前模型" value={model} placeholder="正在读取模型" disabled options={model ? [{ value: model, label: model }] : []} />
    </div>
    <div className="playground-settings-section"><h3>输出配置</h3>
      {parameters.map(parameter => <div className="playground-parameter" key={parameter.key}>
        <div className="parameter-heading"><label htmlFor={`ai-setting-${parameter.key}`}>{parameter.label}<Tooltip title={parameter.tip}><QuestionCircleOutlined aria-label={`${parameter.label}说明`} /></Tooltip></label>
          <InputNumber id={`ai-setting-${parameter.key}`} aria-label={parameter.label} size="small" min={parameter.min} max={parameter.max} step={parameter.step} precision={parameter.key === 'max_tokens' ? 0 : 2}
            value={value[parameter.key]} disabled={disabled} onChange={next => { if (next !== null) onChange({ ...value, [parameter.key]: Math.min(parameter.max, Math.max(parameter.min, next)) }); }} />
        </div>
        <Slider ariaLabelForHandle={`${parameter.label}滑块`} min={parameter.min} max={parameter.max} step={parameter.step} value={value[parameter.key]} disabled={disabled} onChange={next => onChange({ ...value, [parameter.key]: next })} />
        <div className="parameter-range"><span>{parameter.min}</span><span>{parameter.max}</span></div>
      </div>)}
      <Button block icon={<ReloadOutlined aria-hidden />} disabled={disabled} onClick={onReset}>恢复默认参数</Button>
    </div>
  </>;
}
