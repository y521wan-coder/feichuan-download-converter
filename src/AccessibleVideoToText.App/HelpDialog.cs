namespace AccessibleVideoToText.App;

public sealed class HelpDialog : Form
{
    public HelpDialog(string content)
    {
        Text = "飞船下载转换工具使用说明";
        AccessibleName = "飞船下载转换工具使用说明";
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(720, 520);
        Size = new Size(860, 650);
        AutoScaleMode = AutoScaleMode.Dpi;
        ShowInTaskbar = true;

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12),
            ColumnCount = 1,
            RowCount = 2
        };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var text = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Both,
            WordWrap = true,
            AccessibleName = "使用说明正文",
            Text = content
        };
        var close = new Button
        {
            Text = "关闭",
            AutoSize = true,
            DialogResult = DialogResult.OK,
            Anchor = AnchorStyles.Right,
            Margin = new Padding(0, 10, 0, 0)
        };
        layout.Controls.Add(text, 0, 0);
        layout.Controls.Add(close, 0, 1);
        Controls.Add(layout);
        AcceptButton = close;
        CancelButton = close;
        Shown += (_, _) => text.Focus();
    }

    public static string LoadContent()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "使用说明.txt");
        try
        {
            return File.ReadAllText(path);
        }
        catch (IOException)
        {
            return "使用说明文件缺失，请重新安装飞船下载转换工具。";
        }
        catch (UnauthorizedAccessException)
        {
            return "无法读取使用说明文件，请检查安装目录权限。";
        }
    }
}
