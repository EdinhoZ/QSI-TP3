# QSI-TP3

## Introdução: Arquitetura e VNFs

Para este trabalho, decidimos focar-nos nos seguintes aspetos de QoS:

- Classificação
- Controlo de Acesso
- Agendamento
- Policiamento
- Monitorização

O objetivo principal de definir estas VNFs é priorizar dados relacionados com _streaming_ de vídeos.

### VNF de Classificação

A classificação é feita diretamente no _switch_ OVS através da instalação de regras OpenFlow que marcam o campo DSCP dos pacotes (não usa _Scapy_ nem analisa tamanho/TTL). 

A VNF adiciona fluxos que identificam classes de tráfego e atribuem marcas DSCP específicas, por exemplo: RTP em UDP 5004/5005 com DSCP 46 (EF) para _streaming_, HTTP (TCP 80) com DSCP 34 (AF41), DNS com DSCP 8, SSH com DSCP 16 e, por omissão, DSCP 0. As regras são instaladas com prioridade baixa para preservar os fluxos de aprendizagem existentes e encaminhamento normal. 

Esta VNF faz apenas a marcação/classificação; a priorização efetiva depende de outras VNFs na cadeia.

### VNF de Controlo de Acesso

Esta VNF implementa uma _firewall_ baseada em `nftables` que filtra tráfego com base em regras personalizadas. Utiliza uma tabela `inet filter` com uma cadeia personalizada (`VNF-FW`) que é inserida na cadeia `forward` do kernel. As regras podem filtrar por protocolo, endereços IP de origem/destino, portas e valores DSCP, permitindo ações como `accept` (aceitar pacote) ou `drop` (descartar). 

Assim, esta VNF controla quais fluxos podem passar através da rede, permitindo ou bloqueando tráfego específico conforme as regras definidas no ficheiro de configuração. Garante que apenas tráfego autorizado (como RTP ou HTTP para _streaming_) seja permitido.

### VNF de Agendamento

Esta VNF implementa enfileiramento por prioridade usando a disciplina `prio` do `tc`, mapeando valores DSCP para bandas de prioridade. Cria uma _qdisc_ com múltiplas bandas (por omissão 3: 0 = alta, 1 = média, 2 = baixa) e instala filtros `u32` que identificam pacotes pelo campo ToS (derivado do DSCP) e os direcionam para a banda correspondente. 

Pacotes em bandas de maior prioridade são transmitidos primeiro, implementando assim a priorização efetiva do tráfego marcado pela VNF de Classificação. Por exemplo, RTP com DSCP 46 pode ser mapeado para banda 0 (prioridade máxima), HTTP com DSCP 34 para banda 1, e tráfego genérico para banda 2, garantindo que _streaming_ seja servido preferencialmente.

### VNF de Policiamento

Esta VNF usa a ferramenta `tc` (_traffic control_) do Linux para aplicar limitações de débito (_rate limiting_) com base nos valores DSCP marcados anteriormente. Implementa duas abordagens: 

- _ingress policing_ usando filtros `u32` que identificam pacotes por DSCP (convertido para o campo ToS) e aplica ações de _police_ com taxa (_rate_) e _burst_ configuráveis, descartando pacotes que excedam os limites; 
-  _egress shaping_ com disciplina HTB (_Hierarchical Token Bucket_) para controlar a largura de banda de saída. 

A VNF deteta automaticamente a interface de rede e permite configurar políticas diferentes para cada classe de tráfego, limitando o débito de fluxos específicos para simular condições de rede com capacidade restrita.

### VNF de Monitorização

Esta VNF coleta estatísticas de tráfego dos _switches_ OVS usando o comando `ovs-ofctl dump-ports`, processa as métricas de bytes e pacotes recebidos/transmitidos por porta, e exporta-as através de um servidor HTTP compatível com _Prometheus_. As métricas incluem: débito em Mbps (_throughput_), taxa de pacotes por segundo (_packet rate_), e contadores totais de bytes e pacotes. 

A VNF calcula taxas instantâneas baseadas em deltas entre leituras sucessivas, evitando valores negativos quando portas são reiniciadas. O servidor exporta métricas na porta 9100 (por omissão) que podem ser consumidas pelo _Prometheus_ e visualizadas no _Grafana_ para monitorização em tempo real do tráfego na rede.

Assim, a cadeia de VNFs é **Classificação -> Controlo de Acesso -> Agendamento -> Policiamento -> Monitorização**.

## Metodologia

### Topologia

A seguinte topologia serve de base para o trabalho.

![topologia](topologia.png "topologia")

O diagrama foi desenhado no GUI do **CORE**, para efeitos de ilustração, mas a implementação concreta realizada através do API **Mininet** em Python. A intenção é representar uma versão simplificada de uma topologia de um ISP, com _switches_, _routers_ e _hosts_.

### Tráfego de Exemplo

Para gerar tráfego de exemplo, utilizamos também as funcionalidades do **Mininet**. Para fazer isso simulamos as diferentes caraterísticas dos três tipos de tráfego pedidos como exemplo.

- VoIP requer baixa largura de banda, mas precisa de baixa latência e jitter.
- Streaming precisa de uso alto de largura de banda e jitter moderado.
- Dados em massa não permitem perdas.

Com essas carateristicas, fazemos um iperf que obriga o uso específico desses três casos para funcionar como um exemplo válido de tráfego.

- VoIP: iperf -u -b 100K
- Streaming: iperf -u -b 5M
- Transferência de Dados: iperf -t 20

## Resultados



### Discussão



## Conclusão

O diagrama de Gantt abaixo representa o fluxo de trabalho para as restantes semanas, tendo em conta as fases descritas no enunciado, os requisitos e a divisão por elementos de grupo para cada tarefa.

![Gantt](gantt.png "Diagrama de Gantt")
