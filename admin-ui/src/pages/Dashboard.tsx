import { useState, useEffect } from 'react';
import { Card, Row, Col, Statistic, Tag, Spin, Typography, Badge } from 'antd';
import {
  UserOutlined,
  SafetyOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { get } from '../services/api';

const { Title } = Typography;

interface StatsData {
  totalUsers: number;
  totalAllowlisted: number;
  channelDistribution: Record<string, number>;
  channels: Record<string, { configured: boolean }>;
}

const CHANNEL_COLORS: Record<string, string> = {
  telegram: 'blue',
  slack: 'purple',
  feishu: 'green',
};

export default function Dashboard() {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get<StatsData>('/api/stats')
      .then(setStats)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!stats) {
    return <div>Failed to load stats.</div>;
  }

  return (
    <div>
      <Title level={4}>Dashboard</Title>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12}>
          <Card>
            <Statistic
              title="Total Users"
              value={stats.totalUsers}
              prefix={<UserOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12}>
          <Card>
            <Statistic
              title="Allowlisted"
              value={stats.totalAllowlisted}
              prefix={<SafetyOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Title level={5} style={{ marginTop: 24 }}>
        Channel Distribution
      </Title>
      <Card>
        {Object.keys(stats.channelDistribution).length === 0 ? (
          <span>No channel bindings yet.</span>
        ) : (
          Object.entries(stats.channelDistribution).map(([ch, count]) => (
            <Tag
              key={ch}
              color={CHANNEL_COLORS[ch] || 'default'}
              style={{ marginBottom: 8 }}
            >
              {ch}: {count}
            </Tag>
          ))
        )}
      </Card>

      <Title level={5} style={{ marginTop: 24 }}>
        Channel Status
      </Title>
      <Row gutter={[16, 16]}>
        {Object.entries(stats.channels).map(([name, info]) => (
          <Col xs={24} sm={8} key={name}>
            <Card>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                {info.configured ? (
                  <CheckCircleOutlined
                    style={{ color: '#52c41a', fontSize: 20 }}
                  />
                ) : (
                  <CloseCircleOutlined
                    style={{ color: '#bfbfbf', fontSize: 20 }}
                  />
                )}
                <span style={{ textTransform: 'capitalize', fontWeight: 500 }}>
                  {name}
                </span>
                <Tag color={info.configured ? 'green' : 'default'}>
                  {info.configured ? 'Configured' : 'Not Configured'}
                </Tag>
              </div>
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  );
}
