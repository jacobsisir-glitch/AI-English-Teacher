# PPT 课件图片目录

## 使用方法

1. 用 PowerPoint / Keynote / Google Slides 制作课件
2. 将每页导出为 PNG 图片（建议 1920×1080）
3. 放入 `sentence_patterns/` 子目录
4. 确保文件名与 `manifest.json` 中的 `image` 路径一致

## 文件结构示例

```
frontend/slides/
  ├── manifest.json
  ├── README.md
  └── sentence_patterns/
        ├── sp_intro_001.png
        ├── sp_sv_001.png
        ├── sp_sv_quiz_001.png
        └── ...
```

## 占位说明

当前 manifest 指向的 PNG 文件尚不存在。
前端在图片加载失败时会显示友好占位提示，不影响微课教学。
放入真实图片后刷新页面即可自动显示。
