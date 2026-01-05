# Introdução (Objetivo)

A implementação de medidas de Qualidade de Serviço ou _Quality of Service_ (QoS) é um elemento essencial no funcionamento das redes modernas, otimizando os processos intervenientes para atingir o desempenho apropriado de acordo com os requisitos distintos das diversas aplicações que hoje existem. 

Este projeto tem como objetivo desenvolver e testar uma cadeia de _Virtual Network Functions_ (VNFs) capaz de aplicar políticas de gestão de tráfego, introduzindo assim QoS para um objetivo específico numa rede simulada. 

Nomeadamente, o alvo de QoS é o _streaming_ VoIP, especificamente através da priorização de pacotes RTP e, secundariamente, HTTP (usado em algumas aplicações para as interfaces web). A intenção é que, ao enviar pacotes RTP, sejam mantidos os valores pretendidos de débito da conexão e minimizados o _jitter_ e _packet loss_, mesmo com condições adversas na rede.

# Metodologia e Implementação

Nesta secção, além de detalhar as decisões finais sobre a arquitetura e a implementação, é referenciada a evolução das decisões sobre cada etapa, detalhando o processo de desenvolvimento, especialmente em comparação com o relatório submetido para a primeira entrega. 

## Arquitetura e VNFs

Esta solução abrange os seguintes aspetos:

- Classificação
- Controlo de Acesso
- Agendamento
- Policiamento
- Monitorização

As VNFs são implementadas em **Python**.

Inicialmente, o agendamento não seria abordado, mas as VNFs sofreram inúmeras mudanças durante o desenvolvimento e, ao longo deste processo, o _scheduler_ foi capaz de ser implementado. 

### VNF de Classificação (_Classifier_)

A classificação é feita através do comando Linux `iptables`, criando regras na tabela `mangle` que marcam o tráfego RTP com o valor DSCP 46 (_Expedited Forwarding_, ou EF) e HTTP com DSCP 34 (_Assured Forwarding_, AF). Os pacotes são identificados como RTP de acordo com a utilização do protocolo UDP nas portas 16384 a 32767 (comuns para RTP na indústria VoIP) bem como 5004 e 5005, usadas na nossa geração de tráfego simulado para RTP e RTCP respetivamente. Já HTTP é identificado pela porta 80 ou 443 (HTTPS). 

A marcação é feita (na _chain_ `PREROUTING`, ou seja, antes de qualquer decisão de routing) para indicar aos próximos processos na cadeia que estes são os pacotes que devem ser priorizados. Portanto, esta VNF faz apenas a marcação/classificação; a priorização efetiva depende de outras VNFs na cadeia.

O _classifier_ teria inicialmente uma abordagem baseada em **OpenFlow** e **OVS**, marcando múltiplos tipos de tráfego com prioridades diferentes. No entanto, foi decidido que, exceto para o RTP, as prioridades eram supérfluas, tendo sido escolhidas de forma arbitrária. A decisão de abandonar o OpenFlow veio da sua complexidade em comparação com `iptables`, que é melhor documentado e estável/funcional em qualquer router Linux. 

=== VNF de Controlo de Acesso (_Firewall_)

Esta VNF implementa uma _firewall_ baseada em `iptables` que filtra tráfego a partir de regras personalizadas. Constitui uma cadeia personalizada "VNF_FW" na tabela `filter`, inserida na cadeia `FORWARD`.

As regras são as seguintes:
- Permitir conexões previamente estabelecidas (`allow_established`);
- Permitir tráfego marcado como RTP através do DSCP 46, ou das portas 16384 a 32767 caso este falhe (`allow_rtp`);
- Limitar o _rate_ de tráfego UDP não-RTP para 50 pacotes por segundo, descartando o que estiver acima para proteger contra _floods_ UDP ou ataques DDoS (`drop_suspicious_udp`);
- Aceitar todos os outros tipos de tráfego (`default_accept`). 

Uma versão anterior desta VNF filtrava de forma mais complexa, classificando HTTP, RTP, DNS e SSH através de um ficheiro de configuração e descartando qualquer outro tipo de tráfego. Essa implementação foi descartada quando foi feita a decisão de que o propósito da _firewall_ seria proteger contra tráfego UDP potencialmente malicioso, e não abandonar tráfego sem relação com RTP; este tráfego não deve ser descartado, pois compromete o funcionamento de outros serviços na rede.

### VNF de Agendamento (_Scheduler_)

Esta VNF implementa escalonamento hierárquico usando _Hierarchical Token Bucket_ (HTB) do `tc`, que combina reserva de largura de banda com priorização. Cria três classes HTB nas interfaces do núcleo da rede: RTP (30 Mbps garantidos, teto 100 Mbps, prioridade 0), AF (20 Mbps garantidos, teto 80 Mbps, prioridade 1) e _Best-Effort_ (10 Mbps garantidos, teto 100 Mbps, prioridade 2). Filtros `u32` mapeiam DSCP 46 para a classe RTP e DSCP 34 para AF, com todo o resto direcionado para _Best-Effort_. Assim, o tráfego RTP recebe não só prioridade máxima mas também largura de banda garantida, enquanto classes inferiores podem "emprestar" largura de banda não utilizada até aos seus tetos.

A implementação inicial do _scheduler_ utilizava _priority queuing_ mais simples, sem garantias de largura de banda para prevenir _starvation_. Em comparação com o `prio` da versão inicial, HTB utiliza de forma mais eficiente os recursos e não permite que uma classe monopolize a largura de banda, fornecendo um mínimo de 10 Mbps mesmo para o tráfego sem prioridade. 

### VNF de Policiamento (_Policer_)

Esta VNF implementa _ingress policing_ nas interfaces LAN, usando o comando `tc` para proteger o núcleo da rede contra ataques UDP. Cria uma `qdisc` _ingress_ e instala dois filtros `u32`: o primeiro permite tráfego RTP (DSCP 46) sem limites; o segundo aplica _policing_ a todo o tráfego UDP não-RTP, limitando a 20 Mbps com burst de 100 KB e descartando pacotes que excedam esse limite. Esta VNF aplica-se apenas ao tráfego de entrada (_ingress_) vindo das LANs, não realizando controlo de saída (_egress shaping_). Assim, limita as taxas de tráfego em UDP não-RTP enquanto garante passagem livre para _streaming_ RTP.

O _policer_ foi essencialmente dividido da sua implementação inicial para o atual _scheduler_ e _policer_, em que o primeiro é o que agora realiza _egress shaping_, limitando tráfego não-UDP. Existe agora alguma sobreposição entre as funções do _firewall_ e do _policer_, no entanto, o primeiro é mais destinado à proteção contra ataques DoS enquanto que o segundo apenas diminui a carga na rede do tráfego UDP que não é classificado como RTP. 

### VNF de Monitorização (_Monitor_)

Esta VNF coleta estatísticas de tráfego das disciplinas de enfileiramento do kernel Linux usando `tc -s qdisc show`. Processa as métricas de bytes e pacotes recebidos/transmitidos por porta, calcula débito em bps e pacotes por segundo baseado em deltas entre leituras sucessivas, e monitoriza o _backlog_ e _drops_ das filas de tráfego. Todas as métricas são exportadas através de um servidor HTTP compatível com **Prometheus** na porta 9100, permitindo visualização em tempo real no **Grafana**. 

A implementação anterior fazia o mesmo, mas com as métricas obtidas de _switches_ OVS, que já não são utilizados.

## Topologia

A seguinte topologia serve de base para o trabalho.

![Topologia da rede](topologia.png)

O diagrama foi desenhado no GUI do **CORE**, para efeitos de ilustração, mas a implementação concreta foi realizada através da API **Mininet**. A intenção é representar uma versão simplificada de uma topologia de um ISP, com _switches_, _routers_ e _hosts_.

A topologia cria um ambiente com diversas fontes de tráfego, um núcleo de rede com _bottleneck_ de 35 Mbps para induzir congestionamento nos testes, e múltiplos caminhos pelos diversos routers. Pode ser inicializada ao correr o _script_ com o comando `sudo python3 topologiaMininet.py`. 

## Orquestração e Integração na Topologia

A cadeia de VNFs é **Classificação → Controlo de Acesso → Agendamento → Policiamento**, com a **Monitorização** tecnicamente acontecendo por fora da cadeia. 

No _script_ desenvolvido para inicializar as VNFs, `launch_vnfs.py`, a integração na topologia é feita de forma dinâmica com deteção dos _hosts_ Mininet em execução. O _script_, que deve ser executado num terminal separado da topologia, assume que esta está a correr, terminando o processo se não for o caso, e seleciona os _hosts_ alvo conforme argumentos passados na linha de comandos (por omissão, escolhe h1 e h4). A classificação é realizada no _namespace_ de cada _host_ alvo e as regras de agendamento e policiamento são aplicadas nas suas interfaces eth0, enquanto que o _firewall_ e a monitorização correm no _root namespace_, cobrindo os routers.

O _script_ pode ser chamado com argumentos que determinam quais VNFs serão lançadas, permitindo o teste dos efeitos com diferentes combinações de VNFs.