import { useState, useEffect, useCallback } from 'react';
import {
  Table,
  Button,
  Menu,
  Modal,
  Popconfirm,
  Spin,
  Typography,
  Layout,
  message,
  Breadcrumb,
} from 'antd';
import {
  FolderOutlined,
  FileOutlined,
  DeleteOutlined,
  EyeOutlined,
  LinkOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { get, del } from '../services/api';

const { Title, Text } = Typography;
const { Sider, Content } = Layout;

interface FileEntry {
  path: string;
  size: number;
  lastModified: string;
}

interface FileContentResponse {
  content?: string;
  presignedUrl?: string;
  size: number;
}

const TEXT_EXTENSIONS = new Set([
  '.md', '.json', '.txt', '.js', '.ts', '.py', '.yaml', '.yml',
  '.toml', '.cfg', '.ini', '.sh', '.html', '.css', '.xml', '.csv',
]);

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function getExtension(path: string): string {
  const dot = path.lastIndexOf('.');
  return dot >= 0 ? path.substring(dot).toLowerCase() : '';
}

export default function Files() {
  const [namespaces, setNamespaces] = useState<string[]>([]);
  const [nsLoading, setNsLoading] = useState(true);
  const [selectedNs, setSelectedNs] = useState<string | null>(null);

  const [files, setFiles] = useState<FileEntry[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);

  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewContent, setPreviewContent] = useState('');
  const [previewPath, setPreviewPath] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    setNsLoading(true);
    get<{ namespaces: string[] }>('/api/files')
      .then((data) => setNamespaces(data.namespaces))
      .catch(console.error)
      .finally(() => setNsLoading(false));
  }, []);

  const fetchFiles = useCallback((ns: string) => {
    setFilesLoading(true);
    get<{ files: FileEntry[] }>(`/api/files/${ns}`)
      .then((data) => setFiles(data.files))
      .catch(console.error)
      .finally(() => setFilesLoading(false));
  }, []);

  const handleSelectNs = (ns: string) => {
    setSelectedNs(ns);
    fetchFiles(ns);
  };

  const handlePreview = async (ns: string, filePath: string) => {
    const ext = getExtension(filePath);
    if (!TEXT_EXTENSIONS.has(ext)) {
      // Binary file: open presigned URL
      try {
        const data = await get<FileContentResponse>(
          `/api/files/${ns}/${filePath}`
        );
        if (data.presignedUrl) {
          window.open(data.presignedUrl, '_blank');
        }
      } catch {
        message.error('Failed to get file URL');
      }
      return;
    }

    setPreviewPath(filePath);
    setPreviewOpen(true);
    setPreviewLoading(true);
    try {
      const data = await get<FileContentResponse>(
        `/api/files/${ns}/${filePath}`
      );
      if (data.content !== undefined) {
        setPreviewContent(data.content);
      } else if (data.presignedUrl) {
        setPreviewContent('(File too large for preview. Opening in new tab...)');
        window.open(data.presignedUrl, '_blank');
      }
    } catch {
      setPreviewContent('Failed to load file content.');
    }
    setPreviewLoading(false);
  };

  const handleDelete = async (ns: string, filePath: string) => {
    try {
      await del(`/api/files/${ns}/${filePath}`);
      message.success('File deleted');
      fetchFiles(ns);
    } catch {
      message.error('Failed to delete file');
    }
  };

  const columns: ColumnsType<FileEntry> = [
    {
      title: 'Path',
      dataIndex: 'path',
      key: 'path',
      render: (path: string) => (
        <span>
          <FileOutlined style={{ marginRight: 8, color: '#8c8c8c' }} />
          {path}
        </span>
      ),
    },
    {
      title: 'Size',
      dataIndex: 'size',
      key: 'size',
      width: 100,
      render: (size: number) => formatBytes(size),
    },
    {
      title: 'Last Modified',
      dataIndex: 'lastModified',
      key: 'lastModified',
      width: 180,
      render: (v: string) => (v ? new Date(v).toLocaleString() : '-'),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 140,
      render: (_: unknown, record: FileEntry) => {
        const ext = getExtension(record.path);
        const isText = TEXT_EXTENSIONS.has(ext);
        return (
          <span style={{ display: 'flex', gap: 4 }}>
            <Button
              size="small"
              icon={isText ? <EyeOutlined /> : <LinkOutlined />}
              onClick={() =>
                selectedNs && handlePreview(selectedNs, record.path)
              }
            >
              {isText ? 'View' : 'Open'}
            </Button>
            <Popconfirm
              title="Delete this file?"
              onConfirm={() =>
                selectedNs && handleDelete(selectedNs, record.path)
              }
            >
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </span>
        );
      },
    },
  ];

  const breadcrumbItems = [
    { title: 'Files' },
    ...(selectedNs ? [{ title: selectedNs }] : []),
  ];

  return (
    <div>
      <Title level={4}>Files</Title>

      <Layout style={{ background: '#fff', minHeight: 500 }}>
        <Sider
          width={240}
          style={{
            background: '#fff',
            borderRight: '1px solid #f0f0f0',
            overflow: 'auto',
          }}
        >
          <div style={{ padding: '12px 16px', fontWeight: 500 }}>
            Namespaces
          </div>
          {nsLoading ? (
            <div style={{ textAlign: 'center', padding: 20 }}>
              <Spin />
            </div>
          ) : namespaces.length === 0 ? (
            <Text
              type="secondary"
              style={{ display: 'block', padding: '8px 16px' }}
            >
              No namespaces found
            </Text>
          ) : (
            <Menu
              mode="inline"
              selectedKeys={selectedNs ? [selectedNs] : []}
              onClick={({ key }) => handleSelectNs(key)}
              items={namespaces.map((ns) => ({
                key: ns,
                icon: <FolderOutlined />,
                label: ns,
              }))}
            />
          )}
        </Sider>
        <Content style={{ padding: 16 }}>
          <Breadcrumb items={breadcrumbItems} style={{ marginBottom: 16 }} />

          {!selectedNs ? (
            <Text type="secondary">
              Select a namespace from the left panel to browse files.
            </Text>
          ) : (
            <Table
              columns={columns}
              dataSource={files}
              rowKey="path"
              loading={filesLoading}
              pagination={{ pageSize: 50 }}
              size="middle"
            />
          )}
        </Content>
      </Layout>

      {/* File Preview Modal */}
      <Modal
        title={previewPath}
        open={previewOpen}
        onCancel={() => {
          setPreviewOpen(false);
          setPreviewContent('');
          setPreviewPath('');
        }}
        footer={null}
        width={700}
      >
        {previewLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : (
          <pre
            style={{
              maxHeight: 500,
              overflow: 'auto',
              background: '#fafafa',
              padding: 16,
              borderRadius: 4,
              fontSize: 12,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {previewContent}
          </pre>
        )}
      </Modal>
    </div>
  );
}
